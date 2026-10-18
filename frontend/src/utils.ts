import { toast } from "frappe-ui";

// ponytail: no translation catalog yet, only {0} substitution; load frappe's translations when the app ships non-English.
export function __(message: string, ...args: (string | number)[]) {
  return message.replace(/{(\d+)}/g, (match, index) => String(args[Number(index)] ?? match));
}

export function getErrorMessage(error: any, showToast = false) {
  const message = error?.messages?.join(", ") || error?.message || "";
  if (showToast && message) toast.error(message);
  return message;
}

export function categoryLabel(name: string, parentName?: string | null) {
  return parentName ? `${parentName} › ${name}` : name;
}

export const importStatusTheme: Record<string, "gray" | "blue" | "green" | "amber" | "red"> = {
  Draft: "gray",
  Queued: "gray",
  Running: "blue",
  Completed: "green",
  "Completed with Errors": "amber",
  Failed: "red",
};

declare module "vue" {
  interface ComponentCustomProperties {
    __: typeof __;
  }
}
