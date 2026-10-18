import { __, categoryLabel } from "./utils";
import { computed, reactive, ref, type Ref } from "vue";

export interface ImportedArticle {
  article: string;
  title: string;
  category: string;
  category_name: string;
  parent_category_name: string | null;
}

export interface WikiNode {
  key: string;
  title: string;
  is_group: boolean;
  route: string | null;
  published: boolean;
  children: WikiNode[];
  imported: ImportedArticle | null;
}

export interface ArticleCategory {
  name: string;
  category_name: string;
  parent_category: string | null;
}

export type ImportAs = "category" | "subcategory" | "existing" | "merge";

export interface WikiGroup {
  key: string;
  title: string;
  depth: number;
  parent: WikiGroup | null;
  items: (WikiGroup | WikiPage)[];
  // every selectable page beneath the group, nested groups included
  pages: WikiPage[];
  settings: {
    importAs: ImportAs;
    name: string;
    // null follows the nearest ancestor's category
    under: string | null;
    category: string | null;
  };
}

export interface WikiPage {
  route: string;
  title: string;
  published: boolean;
  imported: ImportedArticle | null;
  group: WikiGroup;
}

export type WikiRow =
  { group: WikiGroup; depth: number } | { page: WikiPage; depth: number };

type PlanTarget =
  { name: string; parent: string | null } | { category: string };

// Target key of the pseudo-group holding pages that sit outside any group.
const UNGROUPED = "ungrouped";
// Prefix of target keys that point at an existing destination HD Article Category.
const EXISTING = "existing:";

const isGeneral = (name: string) => name.trim().toLowerCase() === "general";

const isGroup = (item: WikiGroup | WikiPage): item is WikiGroup =>
  "items" in item;

export function useWikiImportPlan(
  nodes: WikiNode[],
  spaceTitle: string,
  categories: Ref<ArticleCategory[]>,
  supportsSubcategories: boolean,
) {
  const { groups, pages } = buildTree(nodes, spaceTitle, supportsSubcategories);
  const groupByKey = new Map(groups.map((g) => [g.key, g]));
  const importedRoutes = new Set(
    pages.filter((p) => p.imported).map((p) => p.route),
  );

  const selected = reactive(
    new Set(pages.filter((p) => p.published).map((p) => p.route)),
  );
  const expanded = reactive(
    new Set(groups.filter((g) => g.depth === 0).map((g) => g.key)),
  );
  const query = ref("");

  const categoryByName = computed(
    () => new Map(categories.value.map((c) => [c.name, c])),
  );

  // One pass over the selection, so every group row reads its count in O(1).
  const selectedCounts = computed(() => {
    const counts = new Map<WikiGroup, number>();
    for (const page of pages) {
      if (!selected.has(page.route)) continue;
      for (let g: WikiGroup | null = page.group; g; g = g.parent) {
        counts.set(g, (counts.get(g) ?? 0) + 1);
      }
    }
    return counts;
  });

  const rows = computed(() => {
    const q = query.value.trim().toLowerCase();
    const groupRows = (group: WikiGroup, parentMatches: boolean): WikiRow[] => {
      const matches = parentMatches || group.title.toLowerCase().includes(q);
      const childRows: WikiRow[] = [];
      for (const item of group.items) {
        if (isGroup(item)) childRows.push(...groupRows(item, matches));
        else if (matches || item.title.toLowerCase().includes(q))
          childRows.push({ page: item, depth: group.depth + 1 });
      }
      if (q && !matches && !childRows.length) return [];
      const open = q || expanded.has(group.key);
      return [{ group, depth: group.depth }, ...(open ? childRows : [])];
    };
    return groups.filter((g) => !g.parent).flatMap((g) => groupRows(g, false));
  });

  function pageMatches(page: WikiPage, q: string) {
    if (page.title.toLowerCase().includes(q)) return true;
    for (let g: WikiGroup | null = page.group; g; g = g.parent) {
      if (g.title.toLowerCase().includes(q)) return true;
    }
    return false;
  }

  // Applies to the filtered pages while a search is active.
  function selectAll(on: boolean) {
    const q = query.value.trim().toLowerCase();
    for (const page of pages) {
      if (!page.published || (q && !pageMatches(page, q))) continue;
      on ? selected.add(page.route) : selected.delete(page.route);
    }
  }

  function toggleGroup(group: WikiGroup) {
    const on = (selectedCounts.value.get(group) ?? 0) < group.pages.length;
    for (const page of group.pages) {
      on ? selected.add(page.route) : selected.delete(page.route);
    }
  }

  function togglePage(page: WikiPage) {
    selected.has(page.route)
      ? selected.delete(page.route)
      : selected.add(page.route);
  }

  function toggleExpanded(group: WikiGroup) {
    expanded.has(group.key)
      ? expanded.delete(group.key)
      : expanded.add(group.key);
  }

  // The top-level target a group's pages end up in or under, if any.
  function topLevelKeyOf(group: WikiGroup): string | null {
    const s = group.settings;
    if (s.importAs === "merge")
      return group.parent ? topLevelKeyOf(group.parent) : null;
    if (s.importAs === "category") return isGeneral(s.name) ? null : group.key;
    if (s.importAs === "subcategory") return parentKeyOf(group);
    const category = categoryByName.value.get(s.category ?? "");
    if (!category) return null;
    if (category.parent_category) return EXISTING + category.parent_category;
    return isGeneral(category.category_name) ? null : group.key;
  }

  function parentKeyOf(group: WikiGroup): string | null {
    const under = group.settings.under;
    if (!under) return group.parent ? topLevelKeyOf(group.parent) : null;
    if (under.startsWith(EXISTING)) {
      const category = categoryByName.value.get(under.slice(EXISTING.length));
      return category && !category.parent_category ? under : null;
    }
    const target = groupByKey.get(under);
    return target && topLevelKeyOf(target) === under ? under : null;
  }

  function targetGroupOf(group: WikiGroup) {
    let g = group;
    while (g.settings.importAs === "merge" && g.parent) g = g.parent;
    return g;
  }

  const plan = computed(() => {
    const targets: Record<string, PlanTarget> = {};
    const errors = new Map<WikiGroup, string>();

    const addTarget = (group: WikiGroup) => {
      if (group.key in targets || errors.has(group)) return;
      const s = group.settings;
      if (s.importAs === "existing") {
        if (s.category) targets[group.key] = { category: s.category };
        else errors.set(group, __("Choose a category"));
        return;
      }
      const name = s.name.trim();
      const isSubcategory = s.importAs === "subcategory";
      const parent = isSubcategory ? parentKeyOf(group) : null;
      const error = !name
        ? __("Enter a name")
        : name.length > 140
          ? __("Use 140 characters or fewer")
          : isSubcategory && !parent
            ? __("Choose a parent category")
            : isSubcategory && isGeneral(name)
              ? __("A subcategory can't be named General")
              : null;
      if (error) {
        errors.set(group, error);
        return;
      }
      if (parent?.startsWith(EXISTING))
        targets[parent] = { category: parent.slice(EXISTING.length) };
      else if (parent) addTarget(groupByKey.get(parent)!);
      targets[group.key] = { name, parent };
    };

    const planPages: { route: string; title: string; target: string }[] = [];
    let updates = 0;
    for (const page of pages) {
      if (!selected.has(page.route)) continue;
      const group = targetGroupOf(page.group);
      addTarget(group);
      planPages.push({
        route: page.route,
        title: page.title,
        target: group.key,
      });
      if (page.imported) updates++;
    }
    return { targets, pages: planPages, errors, updates };
  });

  const createCounts = computed(() => {
    const created = { categories: 0, subcategories: 0 };
    // Updates keep their category, so only targets of new pages (and their parents) get created.
    const { targets, pages: planPages } = plan.value;
    const used = new Set(
      planPages
        .filter((page) => !importedRoutes.has(page.route))
        .map((page) => page.target),
    );
    for (const key of [...used]) {
      const target = targets[key];
      if ("name" in target && target.parent) used.add(target.parent);
    }
    for (const key of used) {
      const target = targets[key];
      if (!("name" in target)) continue;
      if (target.parent) created.subcategories++;
      else if (!isGeneral(target.name)) created.categories++;
    }
    return created;
  });

  // Top-level targets a subcategory can sit under, shared by every group row.
  const parentOptions = computed(() =>
    [
      {
        group: __("In this import"),
        options: groups
          .filter((g) => topLevelKeyOf(g) === g.key)
          .map((g) => ({
            value: g.key,
            label:
              g.settings.importAs === "existing"
                ? categoryByName.value.get(g.settings.category!)!.category_name
                : g.settings.name.trim() || g.title,
          })),
      },
      {
        group: __("Existing categories"),
        options: categories.value
          .filter((c) => !c.parent_category && !isGeneral(c.category_name))
          .map((c) => ({ value: EXISTING + c.name, label: c.category_name })),
      },
    ].filter((g) => g.options.length),
  );

  const categoryOptions = computed(() =>
    categories.value
      .map((c) => ({
        value: c.name,
        label: categoryLabel(
          c.category_name,
          c.parent_category
            ? categoryByName.value.get(c.parent_category)?.category_name
            : undefined,
        ),
      }))
      .sort((a, b) => a.label.localeCompare(b.label)),
  );

  // The subcategory picker shows the auto-picked parent until one is chosen.
  function parentValueOf(group: WikiGroup) {
    return group.settings.under ?? parentKeyOf(group);
  }

  return {
    selected,
    query,
    rows,
    selectedCounts,
    plan,
    createCounts,
    parentOptions,
    categoryOptions,
    selectAll,
    toggleGroup,
    togglePage,
    toggleExpanded,
    expanded,
    parentValueOf,
    selectablePageCount: pages.filter((p) => p.published).length,
  };
}

function buildTree(
  nodes: WikiNode[],
  spaceTitle: string,
  supportsSubcategories: boolean,
) {
  const groups: WikiGroup[] = [];
  const pages: WikiPage[] = [];

  const newGroup = (
    key: string,
    title: string,
    parent: WikiGroup | null,
    name = title,
  ): WikiGroup => {
    const depth = parent ? parent.depth + 1 : 0;
    const group: WikiGroup = {
      key,
      title,
      depth,
      parent,
      items: [],
      pages: [],
      settings: reactive({
        importAs:
          depth === 0
            ? "category"
            : depth === 1 && supportsSubcategories
              ? "subcategory"
              : "merge",
        name,
        under: null,
        category: null,
      }),
    };
    groups.push(group);
    parent?.items.push(group);
    return group;
  };

  const ungrouped = () =>
    groups.find((g) => g.key === UNGROUPED) ??
    newGroup(UNGROUPED, __("Pages outside any group"), null, spaceTitle);

  // ponytail: pages nested under a page are listed in that page's group.
  const walk = (items: WikiNode[], group: WikiGroup | null) => {
    for (const node of items) {
      if (node.is_group) {
        walk(node.children, newGroup(node.key, node.title, group));
        continue;
      }
      const owner = group ?? ungrouped();
      const page: WikiPage = {
        route: node.route!,
        title: node.title,
        published: node.published,
        imported: node.imported,
        group: owner,
      };
      pages.push(page);
      owner.items.push(page);
      if (page.published) {
        for (let g: WikiGroup | null = owner; g; g = g.parent)
          g.pages.push(page);
      }
      walk(node.children, group);
    }
  };
  walk(nodes, null);

  // Keep stray pages first, where the wiki usually puts its intro pages.
  groups.sort(
    (a, b) => Number(b.key === UNGROUPED) - Number(a.key === UNGROUPED),
  );
  return { groups, pages };
}
