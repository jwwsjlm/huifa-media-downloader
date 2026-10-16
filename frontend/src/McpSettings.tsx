import { useEffect, useState } from 'react';
import { Button, Icon } from './components';
import { host } from './transport';
import type { Run } from './App';
import type { Translate } from './i18n';

type Tool = { name: string; description: string; inputSchema: { properties: Record<string, unknown>; required?: string[] }; annotations: { readOnlyHint: boolean } };
type Connection = { url: string; http: unknown; stdio: unknown; codex: string; tools: Tool[] };

export function McpSettings({ run, t }: { run: Run; t: Translate }) {
  const [enabled, setEnabled] = useState(false);
  const [port, setPort] = useState(19492);
  const [busy, setBusy] = useState(false);
  const [ready, setReady] = useState(false);
  const [copied, setCopied] = useState('');
  const [connection, setConnection] = useState<Connection | null>(null);
  const [kind, setKind] = useState<'http' | 'stdio' | 'codex'>('http');
  useEffect(() => { let active = true; run(async () => {
    const state = host.desktop ? await host.command<{ enabled?: boolean; port?: number; connection?: Connection }>('mcp.status') : await webState();
    const value = host.desktop ? await host.command<Connection>('mcp.connection') : state.connection!;
    if (active) { setEnabled(Boolean(state.enabled)); setPort(state.port || 19492); setConnection(value); setReady(true); }
  }); return () => { active = false; }; }, [run]);
  const toggle = () => { setBusy(true); run(async () => { try {
    const state = host.desktop ? await host.command<{ enabled: boolean }>('mcp.configure', { enabled: !enabled, port }) : await webState(!enabled);
    setEnabled(Boolean(state.enabled));
  } finally { setBusy(false); } }); };
  const copy = (text: string, target: string) => run(async () => {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
    else {
      const field = document.createElement('textarea'); field.value = text; field.style.position = 'fixed'; field.style.opacity = '0'; document.body.append(field);
      try { field.select(); if (!document.execCommand('copy')) throw new Error(t('复制失败，请手动选中配置复制。', 'Copy failed. Select and copy the configuration manually.')); } finally { field.remove(); }
    }
    setCopied(target);
  });
  const url = connection?.url || `http://127.0.0.1:${port}/mcp`;
  const config = kind === 'codex' ? connection?.codex || `[mcp_servers.huifa]\nurl = "${url}"\n` : JSON.stringify(connection?.[kind] || { mcpServers: { huifa: { url } } }, null, 2);
  const descriptions: Record<string, string> = {
    search_videos: '搜索 YouTube 视频', get_video_info: '获取标题、作者、描述、时长、章节和字幕语言',
    list_video_formats: '查看画质、编码、音轨和文件大小', list_collection_items: '分页查看播放列表或频道视频',
    get_video_transcript: '获取人工或自动字幕，支持语言、时间范围和分页', get_video_comments: '获取视频评论，支持数量限制和站点支持的排序',
    list_downloads: '分页查看下载任务', get_download: '查看单个任务的状态、进度和错误',
    start_download: '创建视频或音频下载，支持最高、最低和指定格式', control_download: '暂停、继续、取消或重试任务，不删除文件',
  };
  return <section className="mcp-service" aria-label={t('AI 视频服务（MCP）', 'AI video service (MCP)')}>
    <header className="mcp-header"><div className="mcp-title"><Icon name="video" /><h2>{t('AI 视频服务（MCP）', 'AI video service (MCP)')}</h2><span className={`mcp-status ${enabled ? 'running' : ''}`} role="status">{enabled ? t('运行中', 'Running') : t('已停止', 'Stopped')}</span></div>
      <label className="mcp-toggle"><span>{t('开启 MCP 服务', 'Enable MCP service')}</span><input type="checkbox" role="switch" checked={enabled} disabled={busy || !ready} onChange={toggle} /><span className="mcp-switch-track" aria-hidden="true" /></label>
    </header>
    <h3>{t('服务设置', 'Service settings')}</h3>
    <div className="mcp-card mcp-address"><div className="mcp-card-copy"><strong><Icon name="external" />{t('连接地址', 'Connection address')}</strong><p>{host.desktop ? t('仅本机可用，开启服务后将此地址填入 AI 客户端。', 'Local access only. Enable the service and add this URL to your AI client.') : t('与当前 Web 页面共用地址和端口，开启服务后将此地址填入 AI 客户端。', 'Shares this web page’s address and port. Enable the service and add this URL to your AI client.')}</p></div><div className="mcp-address-value"><code>{url}</code><Button icon="copy" disabled={!ready} onClick={() => copy(url, 'url')}>{copied === 'url' ? t('已复制', 'Copied') : t('复制地址', 'Copy URL')}</Button></div></div>
    <div className="mcp-card"><div className="mcp-card-copy"><strong><Icon name="settings" />{t('跟随当前下载设置', 'Use current download settings')}</strong><p>{t('自动使用已保存的 Cookie、代理和下载方案。软件保持运行时，AI 可查询资料并创建、暂停或重试下载任务。', 'Uses saved cookies, proxy and download options. While the app is running, AI can query media and create, pause or retry downloads.')}</p></div></div>
    <h3>{t('接入 AI 客户端', 'Connect an AI client')}</h3>
    <div className="mcp-card"><div className="mcp-card-copy"><strong><Icon name="info" />{t('接入方式', 'How to connect')}</strong><p>{t('在支持 MCP Streamable HTTP 的客户端中填写上方地址即可，无需密钥。也可复制下方 JSON 配置；仅支持 stdio 的客户端请选择对应格式。', 'Use the URL above in a client supporting MCP Streamable HTTP. No key is needed. Alternatively, copy the JSON below; choose stdio for clients requiring that transport.')}</p></div></div>
    <div className="mcp-card mcp-config"><div className="mcp-card-heading"><strong><Icon name="copy" />{t('客户端配置示例', 'Client configuration example')}</strong><Button icon="copy" disabled={!ready} onClick={() => copy(config, 'config')}>{copied === 'config' ? t('已复制', 'Copied') : t('复制配置', 'Copy configuration')}</Button></div><p>{t('配置已按当前连接地址生成，复制到自己的 AI 客户端即可。', 'Generated from the current connection URL. Copy it into your AI client.')}</p><select aria-label={t('客户端配置格式', 'Client configuration format')} value={kind} onChange={event => { setKind(event.target.value as typeof kind); setCopied(''); }}><option value="http">JSON / Streamable HTTP</option>{host.desktop && <option value="stdio">JSON / stdio</option>}<option value="codex">Codex TOML</option></select><pre aria-label={t('MCP 配置示例', 'MCP configuration example')}><code>{config}</code></pre></div>
    <details className="mcp-card mcp-tools"><summary><Icon name="settings" /><strong>{t('可用工具', 'Available tools')}（{connection?.tools?.length || 0}）</strong></summary><p>{t('AI 可调用的全部工具，按资料查询与下载管理分组。', 'All available tools, grouped by research and download management.')}</p>{[true, false].map(readonly => <div className="mcp-tool-group" key={String(readonly)}><h4>{readonly ? t('资料查询 · 只读', 'Research · Read only') : t('下载管理 · 写入', 'Download management · Write')}</h4>{connection?.tools?.filter(tool => tool.annotations.readOnlyHint === readonly).map(tool => <div className="mcp-tool-row" key={tool.name}><code>{tool.name}({Object.keys(tool.inputSchema.properties).map(key => `${key}${tool.inputSchema.required?.includes(key) ? '' : '?'}`).join(', ')})</code><span>{t(descriptions[tool.name] || tool.description, tool.description)}</span></div>)}</div>)}</details>
  </section>;
}

async function webState(enabled?: boolean): Promise<{ enabled: boolean; port?: number; connection: Connection }> {
  const value = enabled === undefined ? await host.mcpStatus<{ enabled: boolean; connection: Connection }>() : await host.mcpConfigure<{ enabled: boolean; connection: Connection }>(enabled);
  const url = new URL('/mcp-server', window.location.origin).href;
  value.connection.url = url;
  value.connection.http = { mcpServers: { huifa: { url } } };
  value.connection.codex = `[mcp_servers.huifa]\nurl = "${url}"\n`;
  return value;
}
