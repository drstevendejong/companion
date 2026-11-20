// theme.js - Persistent UI theme catalog & switcher (shared with future Settings modal)

export const THEME_STORAGE_KEY = "companion.theme";
export const DEFAULT_THEME_ID = "midnight";

export const THEMES = [
  { id: "midnight", label: "Midnight Blue (Default)" },
  { id: "darcula", label: "Darcula (PyCharm)" },
  { id: "nordic-slate", label: "Washed Slate Blue" },
  { id: "light", label: "Crisp Daylight (Light)" },
  { id: "warm-paper", label: "Warm Paper (Light)" },
  { id: "espresso", label: "Espresso & Amber" },
  { id: "forest-night", label: "Forest Night" },
];

const VALID_THEME_IDS = new Set(THEMES.map((t) => t.id));

export function getStoredTheme() {
  try {
    const saved = localStorage.getItem(THEME_STORAGE_KEY);
    if (saved && VALID_THEME_IDS.has(saved)) {
      return saved;
    }
  } catch {
    // Ignore storage access errors in restricted contexts
  }
  return DEFAULT_THEME_ID;
}

export function applyTheme(themeId, persist = true) {
  const id = VALID_THEME_IDS.has(themeId) ? themeId : DEFAULT_THEME_ID;
  document.documentElement.dataset.theme = id;
  if (persist) {
    try {
      localStorage.setItem(THEME_STORAGE_KEY, id);
    } catch {
      // Ignore storage access errors
    }
  }
  const select = document.getElementById("theme-select");
  if (select && select.value !== id) {
    select.value = id;
  }
  return id;
}

export function initTheme() {
  const select = document.getElementById("theme-select");
  const activeId = getStoredTheme();

  if (select) {
    select.innerHTML = "";
    for (const theme of THEMES) {
      const opt = document.createElement("option");
      opt.value = theme.id;
      opt.textContent = theme.label;
      select.appendChild(opt);
    }
    select.value = activeId;
    select.addEventListener("change", () => {
      applyTheme(select.value, true);
    });
  }

  applyTheme(activeId, false);
}
