<template>
  <div
    v-if="!selectablePageCount"
    class="flex flex-col items-center gap-1 py-12 text-center"
  >
    <span class="text-base-medium text-ink-gray-8">
      {{ __("Nothing to import") }}
    </span>
    <span class="text-p-sm text-ink-gray-5">
      {{ __("This space has no published pages yet.") }}
    </span>
  </div>
  <div v-else class="flex flex-col gap-3">
    <div class="flex flex-wrap items-center gap-2">
      <TextInput
        v-model="query"
        :placeholder="__('Filter by title')"
        class="w-60"
        :debounce="150"
      >
        <template #prefix>
          <span class="lucide-search size-4 text-ink-gray-5" />
        </template>
      </TextInput>
      <Button
        variant="ghost"
        :label="__('Select all')"
        @click="selectAll(true)"
      />
      <Button
        variant="ghost"
        :label="__('Select none')"
        @click="selectAll(false)"
      />
      <span class="ms-auto text-p-sm text-ink-gray-5">
        {{
          __("{0} of {1} pages selected", selected.size, selectablePageCount)
        }}
      </span>
    </div>
    <p v-if="!supportsSubcategories" class="text-p-sm text-ink-gray-5">
      {{
        __(
          "This helpdesk doesn't support subcategories; wiki groups become categories."
        )
      }}
    </p>

    <div class="flex flex-col">
      <template v-for="row in rows" :key="rowKey(row)">
        <div
          v-if="'group' in row"
          class="flex flex-col gap-1.5 py-1.5"
          :class="{ 'mt-2': !row.depth }"
          :style="{ paddingInlineStart: `${row.depth * 20}px` }"
        >
          <div class="flex items-center gap-2">
            <button
              class="grid size-5 shrink-0 place-items-center rounded text-ink-gray-5 hover:bg-surface-gray-2"
              :aria-label="__('Expand {0}', row.group.title)"
              :aria-expanded="isOpen(row.group)"
              @click="toggleExpanded(row.group)"
            >
              <span
                class="lucide-chevron-right size-4 transition-transform"
                :class="{ 'rotate-90': isOpen(row.group) }"
              />
            </button>
            <Checkbox
              :model-value="groupState(row.group) === 'all'"
              :indeterminate="groupState(row.group) === 'some'"
              :disabled="!row.group.pages.length"
              :aria-label="row.group.title"
              @update:model-value="toggleGroup(row.group)"
            />
            <button
              class="min-w-0 truncate text-start text-base-medium text-ink-gray-8"
              @click="toggleExpanded(row.group)"
            >
              {{ row.group.title }}
            </button>
            <span class="shrink-0 text-p-sm text-ink-gray-5">
              {{ pageCount(row.group.pages.length) }}
            </span>
            <Select
              v-model="row.group.settings.importAs"
              :options="
                row.group.depth ? nestedImportAsOptions : importAsOptions
              "
              :disabled="groupState(row.group) === 'none'"
              class="ms-auto w-44 shrink-0"
            />
          </div>
          <div
            v-if="
              groupState(row.group) !== 'none' &&
              row.group.settings.importAs !== 'merge'
            "
            class="flex flex-wrap items-center gap-2 ps-[50px]"
          >
            <Combobox
              v-if="row.group.settings.importAs === 'existing'"
              v-model="row.group.settings.category"
              :options="categoryOptions"
              :placeholder="__('Choose a category')"
              class="w-64"
            />
            <template v-else>
              <TextInput
                v-model="row.group.settings.name"
                :placeholder="__('Category name')"
                :aria-label="__('Category name')"
                class="w-56"
              />
              <template v-if="row.group.settings.importAs === 'subcategory'">
                <span class="text-p-sm text-ink-gray-5">{{ __("under") }}</span>
                <Combobox
                  :model-value="parentValueOf(row.group)"
                  :options="parentOptions"
                  :placeholder="__('Choose a category')"
                  class="w-56"
                  @update:model-value="setParent(row.group, $event)"
                />
              </template>
            </template>
          </div>
          <ErrorMessage
            v-if="plan.errors.has(row.group)"
            :message="plan.errors.get(row.group) ?? ''"
            class="ps-[50px]"
          />
        </div>
        <div
          v-else
          class="flex min-h-8 items-center gap-2 pe-1"
          :style="{ paddingInlineStart: `${row.depth * 20 + 28}px` }"
        >
          <Checkbox
            :model-value="selected.has(row.page.route)"
            :disabled="!row.page.published"
            :label="row.page.title"
            @update:model-value="togglePage(row.page)"
          />
          <span class="min-w-0 flex-1 truncate text-p-sm text-ink-gray-4">
            /{{ row.page.route }}
          </span>
          <Badge
            v-if="!row.page.published"
            :label="__('Not published')"
            variant="subtle"
            class="shrink-0"
          />
          <Badge
            v-else-if="row.page.imported"
            :label="
              __(
                'Imported · {0}',
                categoryLabel(
                  row.page.imported.category_name,
                  row.page.imported.parent_category_name
                )
              )
            "
            :title="__('Updates keep the article in its current category')"
            theme="blue"
            variant="subtle"
            class="shrink-0"
          />
        </div>
      </template>
      <span
        v-if="!rows.length"
        class="py-6 text-center text-p-sm text-ink-gray-5"
      >
        {{ __("No pages match “{0}”", query) }}
      </span>
    </div>

    <div
      class="sticky bottom-0 -mx-5 mt-2 flex flex-col gap-2 border-t border-outline-gray-2 bg-surface-base px-5 py-3"
    >
      <div class="flex flex-wrap items-end gap-3">
        <Select
          v-model="articleStatus"
          :label="__('Publish as')"
          :options="statusOptions"
          class="w-32"
        />
        <Select
          v-model="visibility"
          :label="__('Visible to')"
          :options="visibilityOptions"
          class="w-36"
        />
        <Select
          v-if="plan.updates"
          v-model="onExisting"
          :label="__('Already imported')"
          :options="onExistingOptions"
          class="w-32"
        />
        <Button
          variant="solid"
          class="ms-auto"
          :label="importLabel"
          :disabled="
            !plan.pages.length || plan.errors.size > 0 || startImport.loading
          "
          :loading="startImport.loading"
          @click="startImport.loading || startImport.submit()"
        />
      </div>
      <ErrorMessage
        v-if="startImport.error"
        :message="getErrorMessage(startImport.error)"
      />
      <p
        v-else
        class="text-p-sm"
        :class="plan.errors.size ? 'text-ink-red-6' : 'text-ink-gray-6'"
      >
        {{ summary }}
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
import {
  Badge,
  Button,
  Checkbox,
  Combobox,
  ErrorMessage,
  Select,
  TextInput,
  createResource,
} from "frappe-ui";
import { computed, ref } from "vue";
import { useRouter } from "vue-router";
import {
  type ArticleCategory,
  type WikiGroup,
  type WikiNode,
  type WikiRow,
  useWikiImportPlan,
} from "./useWikiImportPlan";
import { __, categoryLabel, getErrorMessage } from "./utils";

const props = defineProps<{
  migrationName: string;
  spaceTitle: string;
  nodes: WikiNode[];
  supportsSubcategories: boolean;
}>();

const router = useRouter();

const categories = createResource({
  url: "wiki_migrator.api.get_categories",
  makeParams: () => ({ name: props.migrationName }),
  auto: true,
  onError: (error: unknown) => getErrorMessage(error, true),
});

const {
  selected,
  query,
  rows,
  selectedCounts,
  expanded,
  plan,
  createCounts,
  parentOptions,
  categoryOptions,
  selectablePageCount,
  selectAll,
  toggleGroup,
  togglePage,
  toggleExpanded,
  parentValueOf,
} = useWikiImportPlan(
  props.nodes,
  props.spaceTitle,
  computed<ArticleCategory[]>(() => categories.data ?? []),
  props.supportsSubcategories
);

const articleStatus = ref("Published");
const visibility = ref("Customers");
const onExisting = ref("Update");

const importAsOptions = [
  { value: "category", label: __("New category") },
  ...(props.supportsSubcategories
    ? [{ value: "subcategory", label: __("New subcategory") }]
    : []),
  { value: "existing", label: __("Existing category") },
];
const nestedImportAsOptions = [
  ...importAsOptions,
  { value: "merge", label: __("Merge into parent") },
];
const statusOptions = [
  { value: "Published", label: __("Published") },
  { value: "Draft", label: __("Draft") },
];
// Same wording as helpdesk's article access modal.
const visibilityOptions = [
  { value: "Public", label: __("Public"), icon: "lucide-globe" },
  { value: "Customers", label: __("Customers"), icon: "lucide-building-2" },
  { value: "Private", label: __("Private"), icon: "lucide-lock" },
];
const onExistingOptions = [
  { value: "Update", label: __("Update") },
  { value: "Skip", label: __("Skip") },
];

const rowKey = (row: WikiRow) =>
  "group" in row ? `group-${row.group.key}` : `page-${row.page.route}`;

const isOpen = (group: WikiGroup) =>
  !!query.value.trim() || expanded.has(group.key);

function groupState(group: WikiGroup) {
  const count = selectedCounts.value.get(group) ?? 0;
  if (!count) return "none";
  return count === group.pages.length ? "all" : "some";
}

const pageCount = (n: number) => (n === 1 ? __("1 page") : __("{0} pages", n));

function setParent(
  group: WikiGroup,
  value: string | number | null | undefined
) {
  group.settings.under = value == null ? null : String(value);
}

const importLabel = computed(() => {
  const n = plan.value.pages.length;
  return n === 1 ? __("Import 1 article") : __("Import {0} articles", n);
});

const summary = computed(() => {
  const { errors, pages, updates } = plan.value;
  if (errors.size) {
    const titles = [...errors.keys()].map((g) => g.title).join(", ");
    return __("Fix the highlighted settings for: {0}", titles);
  }
  if (!pages.length) return __("Select the pages to import.");

  const { categories: newCategories, subcategories } = createCounts.value;
  const newArticles = pages.length - updates;
  const created = [
    newCategories === 1
      ? __("1 category")
      : newCategories && __("{0} categories", newCategories),
    subcategories === 1
      ? __("1 subcategory")
      : subcategories && __("{0} subcategories", subcategories),
    newArticles === 1
      ? __("1 article")
      : newArticles && __("{0} articles", newArticles),
  ].filter(Boolean);
  const parts = created.length ? [__("Creates {0}.", created.join(", "))] : [];
  if (updates) {
    parts.push(
      onExisting.value === "Update"
        ? __(
            "Updates {0} already imported, keeping their current category.",
            updates
          )
        : __("Skips {0} already imported.", updates)
    );
  }
  return parts.join(" ");
});

const startImport = createResource({
  url: "wiki_migrator.api.start_import",
  makeParams: () => ({
    name: props.migrationName,
    plan: {
      article_status: articleStatus.value,
      visibility: visibility.value,
      on_existing: onExisting.value,
      targets: plan.value.targets,
      pages: plan.value.pages,
    },
  }),
  onSuccess: (doc: { name: string }) =>
    router.push({ name: "Run", params: { name: doc.name } }),
  onError: (error: unknown) => getErrorMessage(error, true),
});
</script>
