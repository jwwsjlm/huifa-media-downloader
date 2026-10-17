export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type OptionValue = string | number | boolean | string[];
export type DownloadOptions = Record<string, OptionValue>;
export type Settings = Record<string, Json> & { downloadOptions?: DownloadOptions; uiPreferences?: { deleteMediaOnTaskDelete?: boolean } };
export type TaskStatus = 'queued' | 'probing' | 'downloading' | 'waiting_processing' | 'processing' | 'processing_failed' | 'paused' | 'cancelled' | 'failed' | 'completed' | 'retained';
export type MediaInfo = { width?: number | null; height?: number | null; fps?: number | null; videoCodec?: string | null; audioCodec?: string | null; container?: string | null; size?: number | null; duration?: number | null };
export interface Task {
  id: string;
  url: string;
  title: string;
  status: TaskStatus;
  stage: string;
  priority?: number;
  live?: boolean;
  conversion?: boolean;
  destinationConflict?: { filename: string; files: string[] } | null;
  stepIndex?: number | null;
  stepCount?: number;
  progress: number;
  stageProgress: number | null;
  stageIndeterminate: boolean;
  speed: number | string | null;
  eta: number | string | null;
  error: string | null;
  warnings?: string[];
  hasThumbnail?: boolean;
  kind?: 'media' | 'assets';
  files?: { filename: string; kind: string; size: number }[];
  createdAt: string;
  options: DownloadOptions;
  outputPath?: string;
  size?: number | string | null;
  downloadedBytes?: number | null;
  totalBytes?: number | null;
  totalBytesEstimated?: boolean;
  sourceInfo?: MediaInfo | null;
  mediaInfo?: MediaInfo | null;
}
export interface TaskLogs { text: string; start: number; end: number; total: number; hasMore: boolean; raw: boolean; legacy: boolean }
export interface Media { id: string; taskId: string; title: string; filename: string; size: number; duration: number; hasCover?: boolean; mediaInfo?: MediaInfo | null }
export interface Tool { name: string; available: boolean; version?: string; latest?: string; error?: string; path?: string; platform?: string; asset?: string }
export interface ToolRelease { name: string; channel: string; current: string | null; latest: string; updateAvailable: boolean; platform: string; asset: string }
export interface Snapshot { version: string; tasks: Task[]; media: Media[]; settings: Settings; tools?: Tool[] }
export interface CoreEvent { kind: string; data: Json }
export interface AddReport { ids: string[]; results: { id: string; status: TaskStatus; action: 'added' | 'existing' | 'restarted' }[]; issues?: { url: string; error: string }[]; cancelled?: boolean }
export interface ReleaseStatus { currentVersion: string; latestVersion: string; updateAvailable: boolean; url: string }
export interface Capabilities { desktop: boolean; browserCookies: boolean; remoteBrowser: boolean; updates: boolean; directoryPicker: boolean }
export interface Session { authenticated: boolean; csrfToken?: string; capabilities?: Partial<Capabilities> }
export interface CookieEntry { id: string; name: string; domains?: string[]; count?: number; createdAt?: string; updatedAt?: string }
export type LoginBrowserMode = 'embedded' | 'system';
export interface LoginBrowserStatus { active: boolean; mode: LoginBrowserMode | null; targetUrl?: string | null; preferredUrl?: string; preferredMode?: LoginBrowserMode; preferenceError?: string | null; systemBrowser: string | null; cookie?: CookieEntry | null; saveError?: string | null }
export interface LoginBrowserSaved { cookie: CookieEntry; settings: Settings }
export interface Format { id?: string; formatId?: string; format_id?: string; ext?: string; width?: number; height?: number; fps?: number; vcodec?: string; acodec?: string; abr?: number; filesize?: number; formatNote?: string; format_note?: string; language?: string }
export interface VideoProbe { title: string; formats: Format[]; duration?: number; thumbnail?: string }
export interface CollectionEntry { id?: string; url: string; title: string; index?: number; duration?: number; available?: boolean; error?: string }
export interface CollectionProbe { kind: 'video' | 'collection'; title: string; entries: CollectionEntry[] }
export interface CoverResult { assetId?: string; id?: string; url?: string; filename?: string }
export type Page = 'downloads' | 'completed' | 'settings' | 'mcp' | 'about';
export type Locale = 'zh-CN' | 'en-US';
