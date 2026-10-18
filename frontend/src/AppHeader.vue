<template>
  <header
    class="sticky top-0 z-10 flex h-12 shrink-0 items-center gap-3 border-b border-outline-gray-2 bg-surface-white px-5"
  >
    <Breadcrumbs :items="[{ label: __('Wiki Migrator'), route: { name: 'Connect' } }, ...items]" class="min-w-0" />
    <div class="ms-auto flex shrink-0 items-center gap-2">
      <slot />
    </div>
  </header>
</template>

<script setup lang="ts">
import { Breadcrumbs } from "frappe-ui";

withDefaults(defineProps<{ items?: { label: string }[] }>(), { items: () => [] });
</script>
