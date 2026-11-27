import { Moon, Sun } from "lucide-react";
import { useState } from "react";

const STORAGE_KEY = "blender-mcp-theme";
type Theme = "dark" | "light";

function readTheme(): Theme {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme;
}

export default function StudioThemeToggle() {
  const [theme, setTheme] = useState<Theme>(() => {
    const saved = readTheme();
    applyTheme(saved);
    return saved;
  });

  function toggleTheme() {
    const next: Theme = theme === "dark" ? "light" : "dark";
    setTheme(next);
    document.documentElement.dataset.theme = next;
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // Theme still applies for this session when storage is unavailable.
    }
  }

  const target = theme === "dark" ? "light" : "dark";
  return (
    <button
      type="button"
      className="icon-button"
      onClick={toggleTheme}
      aria-label={`Switch to ${target} theme`}
      data-tooltip={`Switch to ${target} theme`}
      aria-pressed={theme === "light"}
    >
      {theme === "dark" ? <Sun size={16} aria-hidden="true" /> : <Moon size={16} aria-hidden="true" />}
    </button>
  );
}
