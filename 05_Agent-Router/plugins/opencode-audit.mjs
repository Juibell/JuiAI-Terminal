import { appendFile, mkdir } from "node:fs/promises";
import path from "node:path";

export const JuiAudit = async () => ({
  event: async ({ event }) => {
    if (
      ![
        "session.created",
        "session.idle",
        "session.error",
        "permission.asked",
      ].includes(event.type)
    )
      return;
    const directory = process.env.JUI_LOG_DIR;
    if (!directory) return;
    await mkdir(directory, { recursive: true });
    // Do not record prompt text, tool arguments or provider credentials.
    await appendFile(
      path.join(directory, "opencode-events.jsonl"),
      JSON.stringify({ at: new Date().toISOString(), type: event.type }) + "\n",
    );
  },
});
