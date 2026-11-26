using NAudio.Wave;
using NAudio.Wave.SampleProviders;

namespace JuiAI.ControlCenter.Services;

public sealed class AudioService : IDisposable
{
    private WaveInEvent? capture;
    private MemoryStream? recording;
    private WaveFileWriter? writer;
    private TaskCompletionSource<byte[]>? recorded;
    private WaveOutEvent? player;
    private WaveFileReader? reader;
    private Stream? playback;
    public bool Recording => capture != null;
    public IReadOnlyList<string> Inputs => Enumerable.Range(0, WaveIn.DeviceCount).Select(i => WaveIn.GetCapabilities(i).ProductName).ToArray();
    public IReadOnlyList<string> Outputs => Enumerable.Range(0, WaveOut.DeviceCount).Select(i => WaveOut.GetCapabilities(i).ProductName).ToArray();

    public void Start(int device)
    {
        StopPlayback();
        recording = new MemoryStream();
        writer = new WaveFileWriter(new NonClosingStream(recording), new WaveFormat(16000, 16, 1));
        recorded = new TaskCompletionSource<byte[]>(TaskCreationOptions.RunContinuationsAsynchronously);
        capture = new WaveInEvent { DeviceNumber = device, WaveFormat = new WaveFormat(16000, 16, 1), BufferMilliseconds = 100 };
        capture.DataAvailable += (_, e) =>
        {
            if (recording.Length < 16000 * 2 * 40 + 44) writer.Write(e.Buffer, 0, Math.Min(e.BytesRecorded, (int)Math.Max(0, 16000 * 2 * 40 + 44 - recording.Length)));
            else capture?.StopRecording();
        };
        capture.RecordingStopped += (_, e) =>
        {
            writer.Dispose();
            if (e.Exception != null) recorded.TrySetException(e.Exception);
            else recorded.TrySetResult(recording.ToArray());
        };
        capture.StartRecording();
    }
    public async Task<byte[]> Stop()
    {
        if (capture == null || recorded == null) throw new InvalidOperationException("There is no active recording.");
        capture.StopRecording();
        try { return await recorded.Task; }
        finally { capture.Dispose(); capture = null; writer = null; recording?.Dispose(); recording = null; }
    }
    public void Play(byte[] wav, int device)
    {
        StopPlayback();
        playback = new MemoryStream(wav);
        reader = new WaveFileReader(playback);
        player = new WaveOutEvent { DeviceNumber = device };
        player.Init(reader);
        player.Play();
    }
    public void PlayFile(string path, int device)
    {
        StopPlayback();
        try
        {
            playback = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read | FileShare.Delete);
            reader = new WaveFileReader(playback);
            player = new WaveOutEvent { DeviceNumber = device };
            player.Init(new SampleToWaveProvider16(reader.ToSampleProvider()));
            player.Play();
        }
        catch
        {
            StopPlayback();
            throw;
        }
    }
    public void StopPlayback()
    {
        player?.Stop(); player?.Dispose(); player = null;
        reader?.Dispose(); reader = null; playback?.Dispose(); playback = null;
    }
    public static byte[] LoadRecording(string path)
    {
        if (new FileInfo(path).Length > 20_000_000) throw new InvalidOperationException("Audio files must be 20 MB or smaller.");
        using var source = new AudioFileReader(path);
        return NormalizeRecording(source);
    }
    public static byte[] NormalizeWav(byte[] wav)
    {
        using var buffer = new MemoryStream(wav);
        using var source = new WaveFileReader(buffer);
        return NormalizeRecording(source.ToSampleProvider());
    }
    public static (TimeSpan Duration, int SampleRate) DescribeWav(byte[] wav)
    {
        using var stream = new MemoryStream(wav, writable: false);
        using var reader = new WaveFileReader(stream);
        return (reader.TotalTime, reader.WaveFormat.SampleRate);
    }
    private static byte[] NormalizeRecording(ISampleProvider samples)
    {
        if (samples.WaveFormat.Channels == 2) samples = new StereoToMonoSampleProvider(samples);
        if (samples.WaveFormat.Channels != 1) throw new InvalidOperationException("Choose a mono or stereo audio file.");
        var resampled = new WdlResamplingSampleProvider(samples, 16000);
        var pcm = new SampleToWaveProvider16(resampled);
        using var output = new MemoryStream();
        using (var wav = new WaveFileWriter(new NonClosingStream(output), pcm.WaveFormat))
        {
            var buffer = new byte[8192]; int count; long total = 0;
            while ((count = pcm.Read(buffer, 0, buffer.Length)) > 0)
            {
                total += count;
                if (total > 16000 * 2 * 40) throw new InvalidOperationException("Test audio must be 40 seconds or shorter.");
                wav.Write(buffer, 0, count);
            }
        }
        return output.ToArray();
    }
    public void Dispose() { capture?.StopRecording(); capture?.Dispose(); StopPlayback(); }
    private sealed class NonClosingStream(Stream inner) : Stream
    {
        public override bool CanRead => inner.CanRead;
        public override bool CanSeek => inner.CanSeek;
        public override bool CanWrite => inner.CanWrite;
        public override long Length => inner.Length;
        public override long Position { get => inner.Position; set => inner.Position = value; }
        public override void Flush() => inner.Flush();
        public override int Read(byte[] buffer, int offset, int count) => inner.Read(buffer, offset, count);
        public override long Seek(long offset, SeekOrigin origin) => inner.Seek(offset, origin);
        public override void SetLength(long value) => inner.SetLength(value);
        public override void Write(byte[] buffer, int offset, int count) => inner.Write(buffer, offset, count);
    }
}
