export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type OptionValue = string | number | boolean | string[];
export type DownloadOptions = Record<string, OptionValue>;
export type Settings = Record<string, Json> & { downloadOptions?: DownloadOptions };
export type TaskStatus = 'queued' | 'probing' | 'downloading' | 'waiting_processing' | 'processing' | 'processing_failed' | 'paused' | 'cancelled' | 'failed' | 'completed';
export interface Task {
  id: string;
  url: string;
  title: string;
  status: TaskStatus;
  stage: string;
  progress: number;
  stageProgress: number | null;
  stageIndeterminate: boolean;
  speed: number | string | null;
  eta: number | string | null;
  error: string | null;
  createdAt: string;
  options: DownloadOptions;
  outputPath?: string;
  size?: string;
}
export interface Media { id: string; taskId: string; title: string; filename: string; size: number; duration: number; hasCover?: boolean }
export interface Tool { name: string; available: boolean; version?: string; latest?: string; error?: string; path?: string; platform?: string; asset?: string }
export interface ToolRelease { name: string; channel: string; current: string | null; latest: string; updateAvailable: boolean; platform: string; asset: string }
export interface Snapshot { version: string; tasks: Task[]; media: Media[]; settings: Settings; tools: Tool[] }
export interface CoreEvent { kind: string; data: Json }
export interface Capabilities { desktop: boolean; browserCookies: boolean; remoteBrowser: boolean; updates: boolean; directoryPicker: boolean }
export interface Session { authenticated: boolean; csrfToken?: string; capabilities?: Partial<Capabilities> }
export interface CookieEntry { id: string; name: string; domains?: string[]; count?: number; createdAt?: string }
export interface Format { id?: string; formatId?: string; format_id?: string; ext?: string; width?: number; height?: number; fps?: number; vcodec?: string; acodec?: string; abr?: number; filesize?: number; formatNote?: string; format_note?: string; language?: string }
export interface VideoProbe { title: string; formats: Format[]; duration?: number; thumbnail?: string }
export interface CollectionEntry { id?: string; url: string; title: string; index?: number; duration?: number; available?: boolean; error?: string }
export interface CollectionProbe { title: string; entries: CollectionEntry[] }
export interface CoverResult { assetId?: string; id?: string; url?: string; filename?: string }
export type Page = 'downloads' | 'completed' | 'settings' | 'about';
export type Locale = 'zh-CN' | 'en-US';
