import { __ } from "./utils";

export const statusOptions = [
  { value: "Published", label: __("Published") },
  { value: "Draft", label: __("Draft") },
];

// Same wording as helpdesk's article access modal.
export const visibilityOptions = [
  { value: "Public", label: __("Public"), icon: "lucide-globe" },
  { value: "Customers", label: __("Customers"), icon: "lucide-building-2" },
  { value: "Private", label: __("Private"), icon: "lucide-lock" },
];

export const onExistingOptions = [
  { value: "Update", label: __("Update") },
  { value: "Skip", label: __("Skip") },
];
