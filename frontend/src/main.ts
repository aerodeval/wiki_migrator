import { FrappeUI, FrappeUIProvider, frappeRequest, setConfig } from "frappe-ui";
import { createApp, h } from "vue";
import { RouterView, createRouter, createWebHistory } from "vue-router";
import Connect from "./Connect.vue";
import Plan from "./Plan.vue";
import Run from "./Run.vue";
import { __ } from "./utils";
import "./index.css";

setConfig("resourceFetcher", frappeRequest);

const router = createRouter({
  history: createWebHistory("/wiki-migrator"),
  routes: [
    { path: "/", name: "Connect", component: Connect },
    { path: "/plan/:name", name: "Plan", component: Plan, props: true },
    { path: "/run/:name", name: "Run", component: Run, props: true },
    { path: "/:path(.*)*", redirect: "/" },
  ],
});

const app = createApp({ render: () => h(FrappeUIProvider, null, () => h(RouterView)) });
app.config.globalProperties.__ = __;
app.use(FrappeUI).use(router).mount("#app");
