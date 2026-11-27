import { CornerDownLeft, Search } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

export type PaletteCommand = {
  id: string;
  label: string;
  hint?: string;
  keywords?: string;
  run: () => void;
};

/** Global jump-to / action palette (Ctrl/Cmd+K). */
export default function CommandPalette({ commands, onClose }: { commands: PaletteCommand[]; onClose: () => void }) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const returnFocus = useRef<Element | null>(document.activeElement);

  const results = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return commands;
    return commands.filter((c) => `${c.label} ${c.hint ?? ""} ${c.keywords ?? ""}`.toLowerCase().includes(q));
  }, [commands, query]);

  useEffect(() => {
    inputRef.current?.focus();
    const previous = returnFocus.current as HTMLElement | null;
    return () => previous?.focus?.();
  }, []);

  useEffect(() => setActive(0), [query]);

  function run(command: PaletteCommand | undefined) {
    if (!command) return;
    onClose();
    command.run();
  }

  return (
    <div
      className="palette-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        className="palette"
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            onClose();
          } else if (event.key === "ArrowDown") {
            event.preventDefault();
            setActive((i) => (results.length ? (i + 1) % results.length : 0));
          } else if (event.key === "ArrowUp") {
            event.preventDefault();
            setActive((i) => (results.length ? (i - 1 + results.length) % results.length : 0));
          } else if (event.key === "Enter") {
            event.preventDefault();
            run(results[active]);
          } else if (event.key === "Tab") {
            event.preventDefault();
          }
        }}
      >
        <div className="palette-input">
          <Search size={15} aria-hidden="true" />
          <input
            ref={inputRef}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Jump to a page or run an action…"
            aria-label="Search commands"
            aria-controls="palette-list"
            aria-activedescendant={results[active] ? `palette-${results[active].id}` : undefined}
            autoComplete="off"
            spellCheck={false}
          />
          <kbd>Esc</kbd>
        </div>
        <div className="palette-list" id="palette-list" role="listbox" aria-label="Commands">
          {results.length === 0 && <div className="palette-empty">No matching commands.</div>}
          {results.map((command, index) => (
            <div
              key={command.id}
              id={`palette-${command.id}`}
              role="option"
              tabIndex={-1}
              aria-selected={index === active}
              className={index === active ? "is-active" : ""}
              onMouseMove={() => setActive(index)}
              onClick={() => run(command)}
            >
              <span>{command.label}</span>
              {command.hint && <small>{command.hint}</small>}
              {index === active && <CornerDownLeft size={13} aria-hidden="true" />}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
