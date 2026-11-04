<template>
	<div class="flex min-h-screen flex-col">
		<AppHeader :items="[{ label: __('Import from a list') }]" />
		<main class="mx-auto flex w-full max-w-3xl flex-col gap-6 px-5 pt-6">
			<div v-if="migration.error" class="flex flex-col items-start gap-3">
				<ErrorMessage :message="getErrorMessage(migration.error)" />
				<Button :label="__('Back')" :route="{ name: 'Connect' }" />
			</div>
			<template v-else-if="migration.data">
				<div class="flex flex-col gap-1">
					<h1 class="text-lg-semibold text-ink-gray-9">
						{{ __("Import from a list") }}
					</h1>
					<p class="truncate text-p-sm text-ink-gray-5">
						{{ migration.data.wiki_url }} → {{ migration.data.helpdesk_url }}
					</p>
				</div>

				<div class="flex flex-col gap-3">
					<Textarea
						v-model="text"
						:label="__('Category list')"
						:rows="12"
						:placeholder="LIST_EXAMPLE"
					/>
					<p class="text-p-sm text-ink-gray-5">
						{{
							__(
								"Write each category as a ## heading, a subcategory as a bullet, and its articles as links under it. Articles keep the order and the labels of the list. Wiki pages are imported from any space, and links to articles on this helpdesk move those articles into place."
							)
						}}
					</p>
					<div class="flex items-center gap-2">
						<input
							ref="fileInput"
							type="file"
							accept=".md,.txt,text/markdown,text/plain"
							class="hidden"
							:aria-label="__('Open a file')"
							@change="openFile"
						/>
						<Button :label="__('Open a file')" @click="fileInput?.click()" />
						<Button
							:label="__('Preview')"
							:disabled="!text.trim()"
							:loading="preview.loading"
							@click="preview.submit()"
						/>
					</div>
					<ErrorMessage v-if="preview.error" :message="getErrorMessage(preview.error)" />
				</div>

				<template v-if="result">
					<section class="flex flex-col gap-3">
						<h2 class="text-base-semibold text-ink-gray-8">{{ summary }}</h2>
						<ErrorMessage
							v-if="blocked"
							:message="__('This helpdesk doesn\'t support subcategories.')"
						/>
						<p v-if="result.already_imported" class="text-p-sm text-ink-gray-6">
							{{
								__(
									"{0} were imported before. They move to where this list puts them.",
									result.already_imported
								)
							}}
						</p>
						<details v-for="category in result.categories" :key="category.name">
							<summary class="cursor-pointer py-1 text-base-medium text-ink-gray-8">
								{{ category.name }}
								<span class="text-p-sm text-ink-gray-5">
									· {{ pageCount(countOf(category)) }}
								</span>
							</summary>
							<div class="flex flex-col gap-2 ps-4 pt-1">
								<PageList :pages="category.pages" />
								<div
									v-for="subcategory in category.subcategories"
									:key="subcategory.name"
								>
									<span class="text-base-medium text-ink-gray-7">
										{{ subcategory.name }}
									</span>
									<PageList :pages="subcategory.pages" />
								</div>
							</div>
						</details>
					</section>

					<section v-if="result.skipped.length" class="flex flex-col gap-2">
						<h2 class="text-base-semibold text-ink-gray-8">
							{{ __("{0} lines won't be imported", result.skipped.length) }}
						</h2>
						<ul class="flex flex-col">
							<li
								v-for="(row, index) in result.skipped"
								:key="index"
								class="flex items-start justify-between gap-3 border-b border-outline-gray-1 py-1.5 text-p-sm"
							>
								<span class="min-w-0 truncate text-ink-gray-8">{{
									row.title
								}}</span>
								<span class="shrink-0 text-ink-gray-5">{{ row.reason }}</span>
							</li>
						</ul>
					</section>

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
								v-if="result.already_imported"
								v-model="onExisting"
								:label="__('Already imported')"
								:options="onExistingOptions"
								class="w-32"
							/>
							<Button
								variant="solid"
								class="ms-auto"
								:label="importLabel"
								:disabled="!result.page_count || blocked || startImport.loading"
								:loading="startImport.loading"
								@click="startImport.loading || startImport.submit()"
							/>
						</div>
						<ErrorMessage
							v-if="startImport.error"
							:message="getErrorMessage(startImport.error)"
						/>
					</div>
				</template>
			</template>
			<div v-else class="flex justify-center py-12">
				<LoadingIndicator class="w-4" />
			</div>
		</main>
	</div>
</template>

<script setup lang="ts">
import {
	Button,
	ErrorMessage,
	LoadingIndicator,
	Select,
	Textarea,
	createResource,
	usePageMeta,
} from "frappe-ui";
import { computed, defineComponent, h, ref, watch } from "vue";
import { useRouter } from "vue-router";
import AppHeader from "./AppHeader.vue";
import { onExistingOptions, statusOptions, visibilityOptions } from "./importOptions";
import { __, getErrorMessage } from "./utils";

interface ListPage {
	title: string;
	route?: string;
	article?: string;
}

interface ListGroup {
	name: string;
	pages: ListPage[];
	subcategories: ListGroup[];
}

interface ListPreview {
	categories: ListGroup[];
	skipped: { title: string; reason: string }[];
	page_count: number;
	already_imported: number;
	supports_subcategories: boolean;
}

const LIST_EXAMPLE = `## Category
- Subcategory
  - [Article](https://docs.example.com/page)`;

const PageList = defineComponent({
	props: { pages: { type: Array as () => ListPage[], required: true } },
	setup: (props) => () =>
		h(
			"ul",
			{ class: "flex flex-col" },
			props.pages.map((page) =>
				h("li", { class: "flex items-center gap-2 py-0.5 text-p-sm" }, [
					h("span", { class: "min-w-0 truncate text-ink-gray-8" }, page.title),
					h(
						"span",
						{ class: "min-w-0 flex-1 truncate text-ink-gray-4" },
						page.route ? `/${page.route}` : __("Moves this helpdesk's article")
					),
				])
			)
		),
});

const props = defineProps<{ name: string }>();
const router = useRouter();

const text = ref("");
const fileInput = ref<HTMLInputElement | null>(null);
const articleStatus = ref("Published");
const visibility = ref("Customers");
const onExisting = ref("Update");

const migration = createResource({
	url: "wiki_migrator.api.get_import",
	makeParams: () => ({ name: props.name }),
	auto: true,
	onSuccess(doc: { status: string }) {
		if (doc.status !== "Draft") router.replace({ name: "Run", params: { name: props.name } });
	},
});

const preview = createResource({
	url: "wiki_migrator.api.preview_list",
	makeParams: () => ({ name: props.name, text: text.value }),
});
const result = computed<ListPreview | null>(() => preview.data);

// A preview only speaks for the text it read.
watch(text, () => preview.reset());

async function openFile(event: Event) {
	const input = event.target as HTMLInputElement;
	const file = input.files?.[0];
	if (file) text.value = await file.text();
	input.value = "";
}

const countOf = (category: ListGroup) =>
	category.subcategories.reduce((count, d) => count + d.pages.length, category.pages.length);

const pageCount = (n: number) => (n === 1 ? __("1 article") : __("{0} articles", n));

const subcategoryCount = computed(() =>
	(result.value?.categories ?? []).reduce(
		(count, d) => count + d.subcategories.filter((sub) => sub.pages.length).length,
		0
	)
);

const blocked = computed(() => !!subcategoryCount.value && !result.value?.supports_subcategories);

const summary = computed(() =>
	__(
		"{0} in {1} categories and {2} subcategories",
		pageCount(result.value!.page_count),
		result.value!.categories.filter(countOf).length,
		subcategoryCount.value
	)
);

const importLabel = computed(() => {
	const n = result.value!.page_count;
	return n === 1 ? __("Import 1 article") : __("Import {0} articles", n);
});

const startImport = createResource({
	url: "wiki_migrator.api.start_list_import",
	makeParams: () => ({
		name: props.name,
		text: text.value,
		options: {
			article_status: articleStatus.value,
			visibility: visibility.value,
			on_existing: onExisting.value,
		},
	}),
	onSuccess: (doc: { name: string }) => router.push({ name: "Run", params: { name: doc.name } }),
	onError: (error: unknown) => getErrorMessage(error, true),
});

usePageMeta(() => ({ title: __("Import from a list") }));
</script>
