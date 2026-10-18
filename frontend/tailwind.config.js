import frappeUIPreset, { content as frappeUIContent } from "frappe-ui/tailwind";

export default {
	presets: [frappeUIPreset],
	content: ["./index.html", "./src/**/*.{vue,ts}", ...frappeUIContent],
};
