# Huifa Video Downloader language packs

Bundled `zh-CN.json` and `en-US.json` provide the complete Simplified Chinese
and English interfaces. Copy another UTF-8 JSON file into the app-local
`data/languages` directory, select it in Settings, and restart the application.
Use either bundled pack as the schema template. Translation keys are the
English strings passed to the UI. Fixed UI text exists only in JSON language
packs; application source contains one stable key per control. Missing keys
safely fall back to the readable English key.

Language packs apply only to fixed user-interface controls: labels, buttons,
menus, dialog copy, placeholders and tooltips. Service/adaptor output, logs,
database values, paths, URLs, versions and native tool/codec names remain
unchanged and must not import the UI translation helpers.

Required fields: `schema_version` (currently `1`), `locale`, `name`,
`native_name`, `authors`, and a string-to-string `translations` object.
