# Huifa Media Downloader language packs

The React interface bundles `zh-CN.json` and `en-US.json`. Settings offers
Simplified Chinese, English, or the system language. Translation keys are
the English labels passed to `frontend/src/i18n.ts`; missing entries fall back
to the control's Chinese or English label. Edit the bundled JSON and rebuild
the frontend to update translations.

Language packs apply only to fixed user-interface controls: labels, buttons,
menus, dialog copy, placeholders and tooltips. Service/adaptor output, logs,
database values, paths, URLs, versions and native tool/codec names remain
unchanged and must not import the UI translation helpers.

Required fields: `schema_version` (currently `1`), `locale`, `name`,
`native_name`, `authors`, and a string-to-string `translations` object.
