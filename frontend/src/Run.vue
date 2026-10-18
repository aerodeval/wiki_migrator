<template>
  <div class="flex min-h-screen flex-col">
    <AppHeader :items="breadcrumbs">
      <Button
        v-if="wikiImport?.can_retry"
        :label="__('Retry failed')"
        iconLeft="lucide-rotate-ccw"
        :loading="retry.loading"
        @click="retry.submit()"
      />
      <Button :label="__('New migration')" :route="{ name: 'Connect' }" />
      <Button
        v-if="wikiImport"
        variant="solid"
        :label="__('Open knowledge base')"
        :href="`${wikiImport.helpdesk_url}/helpdesk/kb`"
      />
    </AppHeader>

    <div
      v-if="!wikiImport && importDoc.loading"
      class="flex items-center justify-center py-12"
    >
      <LoadingIndicator class="w-4" />
    </div>
    <div v-else-if="!wikiImport" class="mx-auto w-full max-w-3xl px-5 py-6">
      <ErrorMessage :message="getErrorMessage(importDoc.error)" />
    </div>
    <div v-else class="mx-auto flex w-full max-w-3xl flex-col gap-6 px-5 py-6">
      <div class="flex flex-col gap-3">
        <div class="flex items-center gap-2">
          <h1 class="truncate text-lg-semibold text-ink-gray-9">
            {{ wikiImport.space_title || wikiImport.space }}
          </h1>
          <Badge
            :label="__(status!)"
            :theme="importStatusTheme[status!] ?? 'gray'"
            variant="subtle"
            class="shrink-0"
          />
        </div>
        <p class="truncate text-p-sm text-ink-gray-5">
          {{ wikiImport.wiki_url }} → {{ wikiImport.helpdesk_url }}
        </p>
        <Progress :value="progress.percent" size="md" class="mt-1" />
        <div class="flex flex-wrap gap-x-6 gap-y-1 text-p-sm text-ink-gray-6">
          <span>
            {{ __("{0} of {1} done", progress.done, progress.total) }}
          </span>
          <span v-for="stat in stats" :key="stat.label">
            {{ stat.label }}
            <span class="text-ink-gray-8">{{ stat.count }}</span>
          </span>
        </div>
        <ErrorMessage v-if="wikiImport.error" :message="wikiImport.error" />
        <p
          v-if="wikiImport.stale"
          class="rounded-6 bg-surface-amber-1 p-3 text-p-sm text-ink-gray-8"
        >
          {{
            __(
              "This import stopped unexpectedly. Retry to continue with the remaining pages."
            )
          }}
        </p>
      </div>

      <ul class="flex flex-col">
        <li
          v-for="item in items"
          :key="item.wiki_route"
          class="flex items-start gap-3 border-b border-outline-gray-1 py-2.5"
        >
          <span
            class="mt-0.5 size-4 shrink-0"
            :class="itemIcons[item.status]"
            role="img"
            :aria-label="__(item.status)"
          />
          <div class="flex min-w-0 flex-1 flex-col gap-0.5">
            <a
              v-if="item.article_url"
              :href="item.article_url"
              target="_blank"
              rel="noopener"
              class="truncate text-base-medium text-ink-gray-8 hover:underline"
            >
              {{ item.title }}
            </a>
            <span v-else class="truncate text-base-medium text-ink-gray-8">
              {{ item.title }}
            </span>
            <p
              v-if="item.message"
              class="text-p-sm"
              :class="
                item.status === 'Failed' ? 'text-ink-red-6' : 'text-ink-gray-5'
              "
            >
              {{ item.message }}
            </p>
          </div>
          <span
            class="w-40 shrink-0 truncate text-end text-p-sm text-ink-gray-5"
          >
            {{
              item.category_name &&
              categoryLabel(item.category_name, item.parent_category_name)
            }}
          </span>
        </li>
      </ul>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useIntervalFn } from "@vueuse/core";
import {
  Badge,
  Button,
  ErrorMessage,
  LoadingIndicator,
  Progress,
  createResource,
  usePageMeta,
} from "frappe-ui";
import { computed, watch } from "vue";
import AppHeader from "./AppHeader.vue";
import {
  __,
  categoryLabel,
  getErrorMessage,
  importStatusTheme,
} from "./utils";

type ItemStatus = "Pending" | "Imported" | "Updated" | "Skipped" | "Failed";

interface WikiImportItem {
  wiki_route: string;
  title: string;
  status: ItemStatus;
  message: string | null;
  article: string | null;
  article_url: string | null;
  category: string | null;
  category_name: string | null;
  parent_category_name: string | null;
}

interface WikiImport {
  name: string;
  wiki_url: string;
  helpdesk_url: string;
  space: string;
  space_title: string;
  status: string;
  error: string | null;
  running: boolean;
  stale: boolean;
  can_retry: boolean;
  counts: Record<
    "total" | "imported" | "updated" | "skipped" | "failed" | "pending",
    number
  >;
  items: WikiImportItem[];
}

const props = defineProps<{ name: string }>();

const itemIcons: Record<ItemStatus, string> = {
  Pending: "lucide-circle-dashed text-ink-gray-4",
  Imported: "lucide-circle-check text-ink-green-6",
  Updated: "lucide-refresh-cw text-ink-blue-5",
  Skipped: "lucide-circle-minus text-ink-gray-4",
  Failed: "lucide-circle-x text-ink-red-6",
};

const importDoc = createResource({
  url: "wiki_migrator.api.get_import",
  makeParams: () => ({ name: props.name }),
  auto: true,
  onError: (error: unknown) => getErrorMessage(error, true),
});
const wikiImport = computed<WikiImport | null>(() => importDoc.data);

const status = computed(() => wikiImport.value?.status);
const isRunning = computed(() => !!wikiImport.value?.running);

const progress = computed(() => {
  const { total, pending } = wikiImport.value!.counts;
  const done = total - pending;
  return { total, done, percent: total ? Math.round((done / total) * 100) : 0 };
});

const stats = computed(() => {
  const counts = wikiImport.value!.counts;
  return [
    { label: __("Imported"), count: counts.imported },
    { label: __("Updated"), count: counts.updated },
    { label: __("Skipped"), count: counts.skipped },
    { label: __("Failed"), count: counts.failed },
  ];
});

// Failures first; the rest keep the wiki's order.
const items = computed(() =>
  [...(wikiImport.value?.items ?? [])].sort(
    (a, b) => Number(b.status === "Failed") - Number(a.status === "Failed")
  )
);

const retry = createResource({
  url: "wiki_migrator.api.retry_failed",
  makeParams: () => ({ name: props.name }),
  onSuccess: () => importDoc.reload(),
  onError: (error: unknown) => getErrorMessage(error, true),
});

const polling = useIntervalFn(() => importDoc.reload(), 2500, {
  immediate: false,
});
watch(isRunning, (running) => (running ? polling.resume() : polling.pause()), {
  immediate: true,
});

const breadcrumbs = computed(() =>
  wikiImport.value
    ? [{ label: wikiImport.value.space_title || wikiImport.value.space }]
    : []
);

usePageMeta(() => ({
  title: wikiImport.value?.space_title || __("Wiki Migrator"),
}));
</script>
