### Wiki Migrator

Migrate Frappe Wiki spaces into a Frappe Helpdesk knowledge base

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop
bench install-app wiki_migrator
bench build --app wiki_migrator
```

Install it on its own site. It does not need Helpdesk or Wiki on the same bench: it talks to both over REST.

### Usage

1. Open `/wiki-migrator` as a System Manager.
2. Enter the wiki URL. For a private wiki, add the API key and secret of a wiki user who can read the spaces.
3. Enter the helpdesk URL, API key and secret. Generate the key on the helpdesk under User → API Access → Generate Keys. The user needs the Agent Manager role.
4. Choose a space. In the planner, select the pages and choose a category for each wiki group: a new category, an existing category, or, when the helpdesk supports subcategories, a subcategory.
5. Start the import. It runs as a background job, so the site needs a running worker. Pages that fail can be retried from the run page.

A re-run of the same wiki and helpdesk updates or skips the pages it imported before. It does not create duplicates.

To bring the articles up to date after the wiki changes, open the run and click **Sync now**, or turn on **Keep in sync** to sync every hour. A sync updates only the articles whose wiki page changed, so edits on the helpdesk stay until the page changes. It does not import pages added to the wiki later. Start a new migration for those.

Limits:

- It needs Frappe Wiki v3 and imports only published pages.
- Embedded videos become links, because Helpdesk removes iframes from articles.
- A public wiki does not let it copy files under `/private/files`. These stay as links to the wiki and the run shows a warning.

On macOS, start the worker with `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES bench worker`. Otherwise jobs that make HTTPS requests crash.

### Development

```bash
cd apps/wiki_migrator
yarn install
yarn dev    # Vite dev server for frontend/
yarn build  # builds into wiki_migrator/public/frontend
```

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/wiki_migrator
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### License

agpl-3.0
