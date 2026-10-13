type HostEvent = { event: string; data: any };
type PathOptions = { kind: 'directory' | 'file' | 'save'; title?: string; defaultPath?: string; filters?: { name: string; extensions: string[] }[] };
interface Window {
  huifa: {
    environment?: 'desktop' | 'web';
    call(target: string, action: string, ...args: any[]): Promise<any>;
    subscribe(listener: (event: HostEvent) => void): () => void;
    choosePath(options: PathOptions): Promise<string>;
    openExternal(url: string): Promise<void>;
  };
}
