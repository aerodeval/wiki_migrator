<template>
  <div class="flex min-h-screen flex-col">
    <AppHeader />
    <main class="mx-auto flex w-full max-w-2xl flex-col gap-10 px-5 py-8">
      <div class="flex flex-col gap-1.5">
        <h1 class="text-xl-semibold text-ink-gray-9">
          {{ __("Migrate a wiki to Helpdesk") }}
        </h1>
        <p class="text-p-base text-ink-gray-6">
          {{
            __(
              "Copy pages from a Frappe Wiki space into a Frappe Helpdesk knowledge base. You choose the pages and their categories before anything is imported.",
            )
          }}
        </p>
      </div>

      <form v-if="!connection" class="flex flex-col gap-8" novalidate @submit.prevent="submit">
        <section class="flex flex-col gap-4">
          <h2 class="text-base-semibold text-ink-gray-8">{{ __("Wiki (source)") }}</h2>
          <TextInput
            v-model="wikiUrl"
            :label="__('Wiki URL')"
            placeholder="https://docs.example.com/my-space"
            :error="errors.wikiUrl"
            required
            autofocus
          />
          <div class="flex flex-col gap-3">
            <Button
              variant="ghost"
              class="-ms-2 self-start"
              :label="__('Private wiki? Add an API key')"
              :iconRight="showWikiKey ? 'lucide-chevron-up' : 'lucide-chevron-down'"
              :aria-expanded="showWikiKey"
              @click="showWikiKey = !showWikiKey"
            />
            <div v-if="showWikiKey" class="grid gap-3 sm:grid-cols-2">
              <TextInput v-model="wikiApiKey" :label="__('API key')" autocomplete="off" />
              <Password
                v-model="wikiApiSecret"
                :label="__('API secret')"
                :error="errors.wikiKey"
                autocomplete="new-password"
              />
              <p class="text-p-sm text-ink-gray-5 sm:col-span-2">
                {{ __("Use the key of a wiki user who can read the spaces. Public wikis don't need one.") }}
              </p>
            </div>
          </div>
        </section>

        <section class="flex flex-col gap-4">
          <h2 class="text-base-semibold text-ink-gray-8">{{ __("Helpdesk (destination)") }}</h2>
          <TextInput
            v-model="helpdeskUrl"
            :label="__('Helpdesk URL')"
            placeholder="https://support.example.com"
            :error="errors.helpdeskUrl"
            required
          />
          <div class="grid gap-3 sm:grid-cols-2">
            <TextInput
              v-model="helpdeskApiKey"
              :label="__('API key')"
              :error="errors.helpdeskApiKey"
              autocomplete="off"
              required
            />
            <Password
              v-model="helpdeskApiSecret"
              :label="__('API secret')"
              :error="errors.helpdeskApiSecret"
              autocomplete="new-password"
              required
            />
            <p class="text-p-sm text-ink-gray-5 sm:col-span-2">
              {{
                __(
                  "Generate them on the helpdesk under User → API Access → Generate Keys. The user needs the Agent Manager role.",
                )
              }}
            </p>
          </div>
        </section>

        <div class="flex flex-col items-start gap-3">
          <ErrorMessage v-if="connect.error" :message="getErrorMessage(connect.error)" />
          <Button type="submit" variant="solid" :label="__('Connect')" :loading="connect.loading" />
        </div>
      </form>

      <div v-else class="flex flex-col gap-4">
        <div class="flex items-start justify-between gap-3">
          <div class="flex min-w-0 flex-col gap-0.5">
            <span class="text-base-medium text-ink-gray-8">{{ __("Connected") }}</span>
            <span class="truncate text-p-sm text-ink-gray-5">
              {{ hostOf(connection.wiki_url) }} → {{ hostOf(connection.helpdesk_url) }}
            </span>
          </div>
          <Button :label="__('Change')" @click="connect.reset()" />
        </div>
        <p v-if="!connection.spaces.length" class="text-p-base text-ink-gray-6">
          {{ __("This wiki has no spaces to import.") }}
        </p>
        <div v-else class="flex items-end gap-2">
          <Select
            v-model="space"
            :label="__('Space')"
            :options="connection.spaces.map((s) => ({ value: s.id, label: s.title }))"
            :placeholder="__('Choose a space')"
            class="w-72"
          />
          <Button
            variant="solid"
            :label="__('Continue')"
            :disabled="!space"
            @click="openPlan(connection.name, space!)"
          />
        </div>
      </div>

      <section v-if="recent.data?.length" class="flex flex-col gap-2">
        <h2 class="text-base-semibold text-ink-gray-8">{{ __("Recent migrations") }}</h2>
        <router-link
          v-for="row in recent.data"
          :key="row.name"
          :to="{ name: 'Run', params: { name: row.name } }"
          class="-mx-2 flex items-center gap-3 rounded px-2 py-2 hover:bg-surface-gray-1"
        >
          <div class="flex min-w-0 flex-1 flex-col">
            <span class="truncate text-base-medium text-ink-gray-8">{{ row.space_title }}</span>
            <span class="truncate text-p-sm text-ink-gray-5">
              {{ hostOf(row.wiki_url) }} → {{ hostOf(row.helpdesk_url) }}
            </span>
          </div>
          <Badge
            :label="__(row.status)"
            :theme="importStatusTheme[row.status] ?? 'gray'"
            variant="subtle"
            class="shrink-0"
          />
          <span class="w-24 shrink-0 text-end text-p-sm text-ink-gray-5">
            {{ dayjs(row.modified).fromNow() }}
          </span>
        </router-link>
      </section>
    </main>
  </div>
</template>

<script setup lang="ts">
import {
  Badge,
  Button,
  ErrorMessage,
  Password,
  Select,
  TextInput,
  createResource,
  dayjs,
  usePageMeta,
} from "frappe-ui";
import { computed, ref } from "vue";
import { useRouter } from "vue-router";
import AppHeader from "./AppHeader.vue";
import { __, getErrorMessage, importStatusTheme } from "./utils";

interface Connection {
  name: string;
  wiki_url: string;
  helpdesk_url: string;
  spaces: { id: string; title: string; route: string }[];
  space: string | null;
}

const router = useRouter();

const wikiUrl = ref("");
const wikiApiKey = ref("");
const wikiApiSecret = ref("");
const showWikiKey = ref(false);
const helpdeskUrl = ref("");
const helpdeskApiKey = ref("");
const helpdeskApiSecret = ref("");
const space = ref<string | null>(null);
// Field errors show only after the first submit, so an untouched form stays quiet.
const submitted = ref(false);

const errors = computed(() => {
  if (!submitted.value) return {};
  const required = (value: string, message: string) => (value.trim() ? undefined : message);
  return {
    wikiUrl: required(wikiUrl.value, __("Enter the wiki URL")),
    wikiKey:
      !wikiApiKey.value.trim() !== !wikiApiSecret.value
        ? __("Enter both the API key and secret, or neither")
        : undefined,
    helpdeskUrl: required(helpdeskUrl.value, __("Enter the helpdesk URL")),
    helpdeskApiKey: required(helpdeskApiKey.value, __("Enter the API key")),
    helpdeskApiSecret: required(helpdeskApiSecret.value, __("Enter the API secret")),
  };
});

const recent = createResource({ url: "wiki_migrator.api.get_recent", auto: true });

const connect = createResource({
  url: "wiki_migrator.api.connect",
  makeParams: () => ({
    wiki_url: wikiUrl.value.trim(),
    wiki_api_key: wikiApiKey.value.trim() || null,
    wiki_api_secret: wikiApiSecret.value || null,
    helpdesk_url: helpdeskUrl.value.trim(),
    helpdesk_api_key: helpdeskApiKey.value.trim(),
    helpdesk_api_secret: helpdeskApiSecret.value,
  }),
  onSuccess(data: Connection) {
    space.value = data.space ?? (data.spaces.length === 1 ? data.spaces[0]!.id : null);
    if (space.value) openPlan(data.name, space.value);
  },
});
const connection = computed<Connection | null>(() => connect.data);

function submit() {
  submitted.value = true;
  if (Object.values(errors.value).some(Boolean)) return;
  connect.submit();
}

function openPlan(name: string, spaceId: string) {
  router.push({ name: "Plan", params: { name }, query: { space: spaceId } });
}

const hostOf = (url: string) => url?.replace(/^https?:\/\//, "") ?? "";

usePageMeta(() => ({ title: __("Wiki Migrator") }));
</script>
