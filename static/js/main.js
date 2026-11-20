// main.js - Bootstrap modular companion frontend

import { initBridge } from "./bridge.js";
import { initCdp } from "./cdp.js";
import { initChat } from "./chat.js";
import { initComposer } from "./composer.js";
import { initDrawer } from "./drawer.js";
import { initGit } from "./git.js";
import { initLayoutResizers } from "./layout.js";
import { initLifecycle } from "./lifecycle.js";
import { initSessions } from "./sessions.js";
import { initSettings } from "./settings.js";
import { initTheme } from "./theme.js";
import { initToolGates } from "./tool_gates.js";

async function bootstrap() {
  initTheme();
  initLifecycle();
  initLayoutResizers();
  initChat();
  initComposer();
  initBridge();
  initToolGates();
  initDrawer();
  initGit();
  initCdp();
  try {
    await initSettings();
  } catch (err) {
    console.error("Failed to initialize settings:", err);
  }
  await initSessions();
}

bootstrap().catch((err) => {
  console.error("Failed to initialize companion UI:", err);
});
