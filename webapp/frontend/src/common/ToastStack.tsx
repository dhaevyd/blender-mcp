import { X } from "lucide-react";
import { useEffect } from "react";
import type { ToastMessage } from "./notificationEvents";
import "./toast-stack.css";

/** How long a toast stays on screen before it dismisses itself. */
const TOAST_LIFETIME_MS = 6000;

type ToastStackProps = {
  toasts: ToastMessage[];
  onDismiss: (id: string) => void;
};

/** Corner stack of in-app notifications. Each toast expires on its own and can be closed early. */
export default function ToastStack({ toasts, onDismiss }: ToastStackProps) {
  if (toasts.length === 0) return null;
  return (
    <div className="studio-toast-stack" role="status" aria-live="polite">
      {toasts.map((toast) => (
        <ToastCard key={toast.id} toast={toast} onDismiss={onDismiss} />
      ))}
    </div>
  );
}

function ToastCard({ toast, onDismiss }: { toast: ToastMessage; onDismiss: (id: string) => void }) {
  useEffect(() => {
    const timer = window.setTimeout(() => onDismiss(toast.id), TOAST_LIFETIME_MS);
    return () => window.clearTimeout(timer);
  }, [toast.id, onDismiss]);

  return (
    <div className={`studio-toast is-${toast.tone}`}>
      <div className="studio-toast-body">
        <strong>{toast.title}</strong>
        {toast.detail && <span>{toast.detail}</span>}
      </div>
      <button
        type="button"
        className="studio-toast-close"
        onClick={() => onDismiss(toast.id)}
        aria-label="Dismiss notification"
      >
        <X size={14} aria-hidden="true" />
      </button>
    </div>
  );
}
