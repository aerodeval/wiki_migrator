import vue from "@vitejs/plugin-vue";
import frappeui from "frappe-ui/vite";
import { defineConfig } from "vite";

// frontendRoute makes the plugin build into wiki_migrator/public/frontend and
// copy index.html to wiki_migrator/www/wiki-migrator.html.
export default defineConfig({
	plugins: [frappeui({ frontendRoute: "/wiki-migrator" }), vue()],
});
