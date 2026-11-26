"""Windows inference adapter for the pinned official FireRedASR2-LLM code."""

import json
import logging
import sys
from pathlib import Path

import numpy as np


def verify_assets(asr):
    root = Path(asr["model_dir"])
    manifest = json.loads((root / "download-manifest.json").read_text(encoding="utf-8"))
    if (
        manifest["repo"] != "FireRedTeam/FireRedASR2-LLM"
        or manifest["revision"] != asr["revision"]
    ):
        raise RuntimeError("FireRed official model manifest does not match settings")
    files = {entry["path"]: entry for entry in manifest["files"]}
    required = {"model.pth.tar", "asr_encoder.pth.tar", "cmvn.ark"}
    required.update(
        f"Qwen2-7B-Instruct/model-0000{i}-of-00004.safetensors" for i in range(1, 5)
    )
    required.update(
        "Qwen2-7B-Instruct/" + name
        for name in (
            "config.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "model.safetensors.index.json",
        )
    )
    for name in required:
        path = root / name
        if (
            name not in files
            or not path.is_file()
            or path.stat().st_size != files[name]["size"]
        ):
            raise RuntimeError(
                f"FireRed asset is missing/incomplete: {name}; run prepare_model.py"
            )
    return root


def pcm_input(samples):
    # Kaldi features use PCM16 amplitude, not normalized [-1, 1] samples.
    return 16000, np.clip(np.rint(samples * 32768), -32768, 32767).astype(np.int16)


def load_model(asr):
    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForCausalLM, BitsAndBytesConfig
    from transformers.utils.logging import disable_progress_bar

    disable_progress_bar()
    # BF16 inputs are intentionally cast by INT8 kernels; suppress only this spam.
    logging.getLogger("bitsandbytes.autograd._functions").addFilter(
        lambda record: (
            not record.getMessage().startswith("MatMul8bitLt: inputs will be cast")
        )
    )

    root = verify_assets(asr)
    if not torch.cuda.is_available():
        raise RuntimeError(
            "FireRed requires the dedicated CUDA runtime; no CPU fallback is enabled"
        )
    free, _ = torch.cuda.mem_get_info()
    if free < asr["min_free_vram_gib"] * 1024**3:
        raise RuntimeError(
            "Insufficient free VRAM for FireRed; release other GPU models first"
        )
    torch.set_num_threads(asr["num_threads"])
    sys.path.insert(0, asr["source_dir"])
    from fireredasr2s.fireredasr2.data.asr_feat import ASRFeatExtractor
    from fireredasr2s.fireredasr2.models.fireredasr_llm import FireRedAsrLlm
    from fireredasr2s.fireredasr2.models.module.adapter import Adapter
    from fireredasr2s.fireredasr2.tokenizer.llm_tokenizer import LlmTokenizerWrapper

    # This official, SHA-256-verified checkpoint contains Namespace metadata.
    # mmap avoids holding a second full encoder checkpoint in the 32 GB system RAM.
    package = torch.load(
        root / "model.pth.tar", map_location="cpu", mmap=True, weights_only=False
    )
    args = package["args"]
    if args.freeze_llm or not args.use_lora:
        raise RuntimeError(
            "Unsupported FireRed checkpoint: expected the official trained LoRA decoder"
        )
    encoder, encoder_dim = FireRedAsrLlm.load_encoder(str(root / "asr_encoder.pth.tar"))
    encoder = encoder.to(device="cuda", dtype=torch.bfloat16)
    llm = AutoModelForCausalLM.from_pretrained(
        root / "Qwen2-7B-Instruct",
        dtype=torch.bfloat16,
        attn_implementation="sdpa",
        quantization_config=BitsAndBytesConfig(load_in_8bit=True),
        device_map={"": 0},
        local_files_only=True,
    )
    llm = get_peft_model(
        llm,
        LoraConfig(
            r=64,
            lora_alpha=16,
            target_modules=[
                "q_proj",
                "k_proj",
                "v_proj",
                "o_proj",
                "up_proj",
                "gate_proj",
                "down_proj",
            ],
            lora_dropout=0.05,
            task_type="CAUSAL_LM",
        ),
    )
    tokenizer = LlmTokenizerWrapper.build_llm_tokenizer(str(root / "Qwen2-7B-Instruct"))
    llm.config.pad_token_id = tokenizer.pad_token_id
    llm.config.bos_token_id = tokenizer.convert_tokens_to_ids("<|im_start|>")
    llm.config.eos_token_id = tokenizer.convert_tokens_to_ids("<|im_end|>")
    llm.config.default_speech_token_id = tokenizer.convert_tokens_to_ids("<speech>")
    projector = Adapter(
        encoder_dim, llm.config.hidden_size, args.encoder_downsample_rate
    )
    projector = projector.to(device="cuda", dtype=torch.bfloat16)
    model = FireRedAsrLlm(encoder, llm, projector, args.freeze_encoder, args.freeze_llm)
    state = package["model_state_dict"]
    if any(key.startswith("llm.") and "lora_" not in key for key in state):
        raise RuntimeError(
            "Refusing to overwrite the quantized base decoder with non-adapter weights"
        )
    loaded = model.load_state_dict(state, strict=False)
    if loaded.unexpected_keys or any(
        key.startswith(("encoder.", "encoder_projector.")) or "lora_" in key
        for key in loaded.missing_keys
    ):
        raise RuntimeError(
            "FireRed speech encoder/projector/LoRA checkpoint is incomplete"
        )
    model.eval()
    del package, state
    return model, tokenizer, ASRFeatExtractor(str(root / "cmvn.ark"))


def transcribe(model, tokenizer, features, samples, asr):
    import torch
    from fireredasr2s.fireredasr2.tokenizer.llm_tokenizer import LlmTokenizerWrapper

    if len(samples) > 40 * 16000:
        raise ValueError("FireRedASR2-LLM accepts at most 40 seconds per recording")
    feats, lengths, _, _, _ = features([pcm_input(samples)], ["microphone"])
    if feats is None:
        return ""
    input_ids, attention_mask, _, _ = LlmTokenizerWrapper.preprocess_texts(
        [""], tokenizer, max_len=128, decode=True
    )
    # Call the official decoder directly: its high-level wrapper hides exceptions.
    with torch.inference_mode():
        generated = model.transcribe(
            feats.to(device="cuda", dtype=torch.bfloat16),
            lengths.cuda(),
            input_ids.cuda(),
            attention_mask.cuda(),
            beam_size=asr["beam_size"],
            decode_max_len=asr["max_new_tokens"],
            decode_min_len=0,
            repetition_penalty=1.0,
            llm_length_penalty=0.0,
            temperature=1.0,
        )
    return tokenizer.batch_decode(generated, skip_special_tokens=True)[0].strip()


def recognize(samples, asr):
    model, tokenizer, features = load_model(asr)
    return transcribe(model, tokenizer, features, samples, asr)
