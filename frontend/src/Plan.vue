<template>
  <div class="flex min-h-screen flex-col">
    <AppHeader :items="[{ label: spaceTitle || __('Plan') }]" />
    <main class="mx-auto flex w-full max-w-3xl flex-col gap-6 px-5 pt-6">
      <div v-if="migration.error" class="flex flex-col items-start gap-3">
        <ErrorMessage :message="getErrorMessage(migration.error)" />
        <Button :label="__('Back')" :route="{ name: 'Connect' }" />
      </div>
      <template v-else-if="migration.data">
        <div class="flex items-start justify-between gap-3">
          <div class="flex min-w-0 flex-col gap-1">
            <h1 class="truncate text-lg-semibold text-ink-gray-9">{{ spaceTitle }}</h1>
            <p class="truncate text-p-sm text-ink-gray-5">
              {{ migration.data.wiki_url }} → {{ migration.data.helpdesk_url }}
            </p>
          </div>
          <Button :label="__('Change')" :route="{ name: 'Connect' }" />
        </div>

        <p v-if="!space" class="text-p-base text-ink-gray-6">
          {{ __("Choose a space to import from.") }}
        </p>
        <div v-else-if="tree.loading" class="flex justify-center py-12">
          <LoadingIndicator class="w-4" />
        </div>
        <div v-else-if="tree.error" class="flex flex-col items-start gap-3 py-6">
          <ErrorMessage :message="getErrorMessage(tree.error)" />
          <Button :label="__('Try again')" @click="tree.fetch()" />
        </div>
        <WikiImportPlanner
          v-else-if="tree.data"
          :key="space"
          :migration-name="name"
          :space-title="spaceTitle"
          :nodes="tree.data.nodes"
          :supports-subcategories="migration.data.supports_subcategories"
        />
      </template>
      <div v-else class="flex justify-center py-12">
        <LoadingIndicator class="w-4" />
      </div>
    </main>
  </div>
</template>

<script setup lang="ts">
import { Button, ErrorMessage, LoadingIndicator, createResource, usePageMeta } from "frappe-ui";
import { computed, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import AppHeader from "./AppHeader.vue";
import WikiImportPlanner from "./WikiImportPlanner.vue";
import { __, getErrorMessage } from "./utils";

const props = defineProps<{ name: string }>();
const route = useRoute();
const router = useRouter();

const migration = createResource({
  url: "wiki_migrator.api.get_import",
  makeParams: () => ({ name: props.name }),
  auto: true,
  onSuccess(doc: { status: string }) {
    if (doc.status !== "Draft") router.replace({ name: "Run", params: { name: props.name } });
  },
});

// Only a Draft is planned; any other status redirects to its run.
const space = computed<string | null>(() =>
  migration.data?.status === "Draft" ? (route.query.space as string) || migration.data.space : null,
);

const tree = createResource({
  url: "wiki_migrator.api.get_tree",
  makeParams: () => ({ name: props.name, space: space.value }),
});
watch(space, (id) => id && tree.fetch(), { immediate: true });

const spaceTitle = computed(() => tree.data?.space?.title ?? migration.data?.space_title ?? "");

usePageMeta(() => ({ title: spaceTitle.value || __("Wiki Migrator") }));
</script>
