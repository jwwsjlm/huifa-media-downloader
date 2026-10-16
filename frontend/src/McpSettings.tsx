import { useEffect, useState } from 'react';
import { Button, Field, Icon } from './components';
import { host } from './transport';
import type { Run } from './App';
import type { Translate } from './i18n';
import { capabilityCoverage, mcpGroups } from './mcpTools';

type Tool = { name: string; description: string; inputSchema: { properties: Record<string, unknown>; required?: string[] }; annotations: { readOnlyHint: boolean } };
type Connection = { url: string; http: unknown; stdio: unknown; codex: string; tools: Tool[] };
type McpState = { enabled: boolean; autoStart: boolean; port?: number; connection?: Connection };
type AccessState = { account: string; defaultPassword: boolean };

export function McpSettings({ run, t }: { run: Run; t: Translate }) {
  const [enabled, setEnabled] = useState(false);
  const [autoStart, setAutoStart] = useState(false);
  const [port, setPort] = useState(19492);
  const [busy, setBusy] = useState(false);
  const [ready, setReady] = useState(false);
  const [copied, setCopied] = useState(''); const [copying, setCopying] = useState('');
  const [connection, setConnection] = useState<Connection | null>(null);
  const [kind, setKind] = useState<'http' | 'stdio' | 'codex'>('http');
  const [search, setSearch] = useState('');
  const [access, setAccess] = useState<AccessState | null>(null);
  const [currentPassword, setCurrentPassword] = useState(''); const [newPassword, setNewPassword] = useState(''); const [confirmation, setConfirmation] = useState('');
  const [passwordBusy, setPasswordBusy] = useState(false); const [passwordMessage, setPasswordMessage] = useState('');
  useEffect(() => { let active = true; run(async () => {
    const [state, value, credentials] = await Promise.all([
      host.desktop ? host.command<McpState>('mcp.status') : webState(),
      host.desktop ? host.command<Connection>('mcp.connection') : Promise.resolve(null),
      host.command<AccessState>('access.status'),
    ]);
    if (active) { setEnabled(Boolean(state.enabled)); setAutoStart(Boolean(state.autoStart)); setPort(state.port || 19492); setConnection(value || state.connection!); setAccess(credentials); setReady(true); }
  }); return () => { active = false; }; }, [run]);
  const configure = (config: { enabled?: boolean; autoStart?: boolean }) => { setBusy(true); run(async () => { try {
    const state = host.desktop ? await host.command<McpState>('mcp.configure', config) : await webState(config);
    setEnabled(Boolean(state.enabled)); setAutoStart(Boolean(state.autoStart));
  } finally { setBusy(false); } }); };
  const changePassword = () => { setPasswordBusy(true); setPasswordMessage(''); run(async () => { try {
    if (newPassword !== confirmation) throw new Error(t('两次输入的新密码不一致。', 'New passwords do not match.'));
    await host.command('access.change_password', { currentPassword, newPassword });
    setCurrentPassword(''); setNewPassword(''); setConfirmation(''); setAccess(value => value && { ...value, defaultPassword: newPassword === 'admin' });
    setPasswordMessage(t('访问密码已更新。', 'Access password updated.'));
    if (!host.desktop) window.dispatchEvent(new Event('huifa-session-expired'));
  } finally { setPasswordBusy(false); } }); };
  const copy = (text: string, target: string) => { setCopying(target); run(async () => {
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
      else {
        const field = document.createElement('textarea'); field.value = text; field.style.position = 'fixed'; field.style.opacity = '0'; document.body.append(field);
        try { field.select(); if (!document.execCommand('copy')) throw new Error(t('复制失败，请手动选中配置复制。', 'Copy failed. Select and copy the configuration manually.')); } finally { field.remove(); }
      }
      setCopied(target);
    } finally { setCopying(''); }
  }); };
  const url = connection?.url || `http://127.0.0.1:${port}/mcp`;
  const config = kind === 'codex' ? connection?.codex || `[mcp_servers.huifa]\nurl = "${url}"\nhttp_headers = { Authorization = "Bearer <YOUR_PASSWORD>" }\n` : JSON.stringify(connection?.[kind] || { mcpServers: { huifa: { url, headers: { Authorization: 'Bearer <YOUR_PASSWORD>' } } } }, null, 2);
  const query = search.trim().toLocaleLowerCase();
  const groups = mcpGroups.map(group => ({ ...group, tools: group.tools.flatMap(help => {
    const tool = connection?.tools.find(item => item.name === help.name);
    return tool && (!query || [help.name, ...help.title, ...help.description, tool.description].join(' ').toLocaleLowerCase().includes(query)) ? [{ ...help, tool }] : [];
  }) })).filter(group => group.tools.length);
  const visibleCount = groups.reduce((count, group) => count + group.tools.length, 0);
  const statuses = { available: t('已开放', 'Exposed through MCP'), site: t('按站点提供', 'Site dependent'), managed: t('由应用管理', 'App managed'), closed: t('未开放', 'Not exposed') };
  return <section className="mcp-service" aria-label={t('AI 媒体服务（MCP）', 'AI media service (MCP)')}>
    <header className="mcp-header"><div className="mcp-title"><Icon name="video" /><h2>{t('AI 媒体服务（MCP）', 'AI media service (MCP)')}</h2><span className={`mcp-status ${enabled ? 'running' : ''}`} role="status">{enabled ? t('运行中', 'Running') : t('已停止', 'Stopped')}</span></div>
      <label className="mcp-toggle"><span>{t('开启 MCP 服务', 'Enable MCP service')}</span><input type="checkbox" role="switch" checked={enabled} disabled={busy || !ready} onChange={() => configure({ enabled: !enabled })} /><span className="mcp-switch-track" aria-hidden="true" /></label>
    </header>
    <h3>{t('服务设置', 'Service settings')}</h3>
    <div className="mcp-card"><label className="mcp-toggle"><span>{t('应用启动时自动开启 MCP', 'Start MCP automatically with the app')}</span><input type="checkbox" role="switch" checked={autoStart} disabled={busy || !ready} onChange={() => configure({ autoStart: !autoStart })} /><span className="mcp-switch-track" aria-hidden="true" /></label><p className="hint">{t('仅影响下次启动，不改变当前服务状态。', 'Applies on the next app start and keeps the current service state.')}</p></div>
    <div className="mcp-card mcp-address"><div className="mcp-card-copy"><strong><Icon name="external" />{t('连接地址', 'Connection address')}</strong><p>{t('开启服务后，把此地址填入 AI 客户端。', 'Enable the service, then add this URL to your AI client.')}</p><p className="hint">{host.desktop ? t('桌面服务仅接受本机连接。网页模式与当前页面共用地址和端口。', 'Desktop service accepts local connections only. Web mode shares the current page address and port.') : t('网页模式与当前页面共用地址和端口。', 'Web mode shares the current page address and port.')}</p></div><div className="mcp-address-value"><code>{url}</code><Button icon="copy" size="small" disabled={!ready || Boolean(copying)} onClick={() => copy(url, 'url')}>{copying === 'url' ? t('复制中…', 'Copying…') : copied === 'url' ? t('已复制', 'Copied') : t('复制地址', 'Copy URL')}</Button></div></div>
    <div className="mcp-card"><div className="mcp-card-copy"><strong><Icon name="settings" />{t('跟随当前下载设置', 'Use current download settings')}</strong><p>{t('使用已保存的 Cookie、代理和下载方案。', 'Uses saved cookies, proxy and download options.')}</p><p className="hint">{t('应用运行期间，AI 可以查询资料并创建、暂停或重试下载任务。', 'While the app is running, AI can query media and create, pause or retry downloads.')}</p></div></div>
    <form className="mcp-card" onSubmit={event => { event.preventDefault(); changePassword(); }}>
      <strong><Icon name="settings" />{t('共享访问密码', 'Shared access password')}</strong>
      <p>{t('网页登录和 HTTP MCP Bearer 使用同一密码；默认密码为 admin。', 'Web sign-in and HTTP MCP Bearer use the same password; the default password is admin.')}</p>
      <p className="hint">{t('网页登录账号', 'Web account')}: {access?.account || 'admin'} · {access?.defaultPassword ? t('当前仍在使用默认密码', 'Currently using the default password') : t('已设置自定义密码', 'Custom password set')}{!host.desktop && t('；修改后需要重新登录。', '; sign in again after changing it.')}</p>
      <div className="form-grid">
        <Field label={t('当前密码', 'Current password')}><input type="password" autoComplete="current-password" required value={currentPassword} disabled={passwordBusy || !ready} onChange={event => setCurrentPassword(event.target.value)} /></Field>
        <Field label={t('新密码', 'New password')}><input type="password" autoComplete="new-password" required value={newPassword} disabled={passwordBusy || !ready} onChange={event => setNewPassword(event.target.value)} /></Field>
        <Field label={t('确认新密码', 'Confirm new password')}><input type="password" autoComplete="new-password" required value={confirmation} disabled={passwordBusy || !ready} onChange={event => setConfirmation(event.target.value)} /></Field>
      </div>
      <Button type="submit" disabled={passwordBusy || !ready || !currentPassword || !newPassword || !confirmation}>{passwordBusy ? t('正在更新密码…', 'Updating password…') : t('更新访问密码', 'Update access password')}</Button>
      {passwordMessage && <p role="status">{passwordMessage}</p>}
    </form>
    <h3>{t('接入 AI 客户端', 'Connect an AI client')}</h3>
    <div className="mcp-card"><div className="mcp-card-copy"><strong><Icon name="info" />{t('接入方式', 'How to connect')}</strong><p>{t('支持 MCP Streamable HTTP 的客户端直接填写上方地址。', 'Clients supporting MCP Streamable HTTP can use the URL above.')}</p><p className="hint">{t('HTTP 请求需带 Authorization: Bearer <访问密码>。配置示例中的密码为占位符；仅支持 stdio 的客户端请选择对应格式。', 'HTTP requests require Authorization: Bearer <YOUR_PASSWORD>. Replace the password placeholder in the example; choose stdio for clients that require it.')}</p></div></div>
    <div className="mcp-card mcp-config"><div className="mcp-card-heading"><strong><Icon name="copy" />{t('客户端配置示例', 'Client configuration example')}</strong><Button icon="copy" size="small" disabled={!ready || Boolean(copying)} onClick={() => copy(config, 'config')}>{copying === 'config' ? t('复制中…', 'Copying…') : copied === 'config' ? t('已复制', 'Copied') : t('复制配置', 'Copy configuration')}</Button></div><p>{t('按当前连接地址生成，复制到 AI 客户端即可。', 'Generated from the current connection URL. Copy it into your AI client.')}</p><select aria-label={t('客户端配置格式', 'Client configuration format')} value={kind} onChange={event => { setKind(event.target.value as typeof kind); setCopied(''); }}><option value="http">JSON / Streamable HTTP</option>{host.desktop && <option value="stdio">JSON / stdio</option>}<option value="codex">Codex TOML</option></select><pre aria-label={t('MCP 配置示例', 'MCP configuration example')}><code>{config}</code></pre></div>
    <section className="mcp-card mcp-tools" aria-label={t('可用工具', 'Available tools')}>
      <div className="mcp-card-heading"><strong><Icon name="settings" />{t('可用工具', 'Available tools')}（{connection?.tools?.length || 0}）</strong><span className="muted">{query ? `${visibleCount} / ${connection?.tools.length || 0}` : t('支持 yt-dlp 多站点媒体', 'Multi-site media through yt-dlp')}</span></div>
      <p>{t('按功能查找工具。字幕、评论、直播和其他资料以站点实际提供的数据为准。', 'Find tools by capability. Subtitles, comments, live media and other metadata depend on the source.')}</p>
      <label className="mcp-tool-search"><Icon name="search" /><input type="search" aria-label={t('搜索工具名称或功能', 'Search tool names or capabilities')} placeholder={t('搜索工具名称或功能', 'Search tool names or capabilities')} value={search} onChange={event => setSearch(event.target.value)} /></label>
      {groups.map((group, index) => <details className="mcp-tool-group" key={`${group.title[1]}-${Boolean(query)}`} open={Boolean(query) || index === 0}>
        <summary><strong>{t(...group.title)}</strong><span className="muted">{group.tools.length}</span></summary>
        {group.tools.map(({ tool, ...help }) => <article className="mcp-tool-row" key={tool.name}>
          <div className="mcp-tool-heading"><strong>{t(...help.title)}</strong><span className="badge">{tool.annotations.readOnlyHint ? t('只读', 'Read only') : t('写入', 'Write')}</span>{help.site && <span className="badge">{t('按站点提供', 'Site dependent')}</span>}</div>
          <p>{t(...help.description)}</p>
          <details className="mcp-tool-params"><summary><code>{tool.name}</code><span>{t('参数与示例', 'Parameters & example')}</span></summary>
            <code className="mcp-signature">{tool.name}({Object.keys(tool.inputSchema.properties).map(key => `${key}${tool.inputSchema.required?.includes(key) ? '' : '?'}`).join(', ')})</code>
            <h5>{t('输入参数', 'Input parameters')}</h5><pre>{JSON.stringify(tool.inputSchema, null, 2)}</pre>
            <h5>{t('调用示例', 'Example call')}</h5><pre>{JSON.stringify(help.example, null, 2)}</pre>
          </details>
        </article>)}
      </details>)}
      {ready && !visibleCount && <p role="status">{query ? t('没有匹配的工具。', 'No matching tools.') : t('当前配置没有可用工具。', 'No tools are available in this configuration.')}</p>}
    </section>
    <details className="mcp-card mcp-coverage"><summary><strong>{t('yt-dlp 功能覆盖', 'yt-dlp capability coverage')}</strong></summary><p>{t('功能以当前引擎版本和应用接口为准。站点列表不保证每项功能都可用。', 'Capabilities follow the installed engine and app interfaces. A listed site may not support every feature.')}</p><div className="mcp-coverage-list">{capabilityCoverage.map(item => <div key={item.title[1]}><strong>{t(...item.title)}</strong><span className="badge">{statuses[item.status]}</span><p>{t(...item.detail)}</p></div>)}</div></details>
  </section>;
}

async function webState(config?: { enabled?: boolean; autoStart?: boolean }): Promise<McpState & { connection: Connection }> {
  const value = config === undefined ? await host.mcpStatus<McpState & { connection: Connection }>() : await host.mcpConfigure<McpState & { connection: Connection }>(config);
  const url = new URL('/mcp-server', window.location.origin).href;
  value.connection.url = url;
  value.connection.http = { mcpServers: { huifa: { url, headers: { Authorization: 'Bearer <YOUR_PASSWORD>' } } } };
  value.connection.codex = `[mcp_servers.huifa]\nurl = "${url}"\nhttp_headers = { Authorization = "Bearer <YOUR_PASSWORD>" }\n`;
  return value;
}
