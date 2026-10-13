# Electron migration acceptance map

The Electron desktop uses the existing Python services and presentation models. The current `python -m app.main` entry remains QML while integrated acceptance is underway; Electron runs from `desktop` with `npm start`. No renderer receives SQLite, secrets, filesystem or shell access.

| Surface | Required actions and state | Contract target |
| --- | --- | --- |
| Downloads | batch plan, quick options, all-history search/filter/sort, stable ID selection, real stage progress/wait reason, start/pause/cancel/retry/redownload, processing retry, convert, source/folder copy, result reveal, task logs, delete/cleanup | app / tasks |
| Collection | probe/select/details, whole collection search/filter/sort, individual/all selection, confirm/cancel, child progress | app / collection |
| Format | video/audio mode, output audio format, complete format list, select/cancel | app / formats |
| Accounts | account name, distribution target, login/check, save | accounts |
| Completed | full catalog search/filter, explicit result reveal, cover studio, publish editor, scoped publish queue | completed / app |
| Publish | search/status/media scope, run/retry, result, platform/account/title/description/topics/source/visibility/collection/playlist/partition/schedule/dual thumbnails | publish / draft / app |
| Cover studio | preview/crop/fit/focus/preset/quality, JPEG export, image copy, original restore, save defaults, AI generate/cancel | cover |
| Settings | every existing setting and advanced download option, native path picking, secret save/clear, cookies/import/browser login/view, language/theme, notifications | settings / app |
| Updates | application check/download/cancel/install, runtime check/install/checksum confirmation, GitHub routes/probe/save, update shutdown | updates / routes |
| About | version, sites/search, diagnostics export/reveal, logs/language folder | sites / app |
| Lifecycle | restore/collection resume, local-history prompt, DB recovery prompt, single instance, tray/notification routing, persistence before exit | backend / Electron main |
| Distribution | frontend executable and backend side by side in Velopack current, same pack ID/credential service/data migration/receipt semantics, installer and portable | build scripts |

Validation must include Python isolated tests, TypeScript build, a real Electron launch against isolated data, durable split-queue/failure/retry checks, and package/data/update checks. A page rendering alone does not establish parity.
