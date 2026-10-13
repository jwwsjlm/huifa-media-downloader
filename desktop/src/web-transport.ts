(() => {
  if (window.huifa) return;
  let csrf = '';
  const listeners = new Set<(event: HostEvent) => void>();
  const dispatch = (event: HostEvent) => { for (const listener of listeners) listener(event); };
  async function responseData(response: Response): Promise<any> {
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || `Request failed (${response.status})`);
    return value;
  }
  const ready = (async () => {
    const session = await fetch('/api/session', { credentials: 'same-origin' });
    if (session.ok) { csrf = (await responseData(session)).csrf; return; }
    await new Promise<void>(resolve => {
      const overlay = document.createElement('div'); overlay.className = 'web-login';
      const form = document.createElement('form'); form.className = 'panel web-login-form';
      const title = document.createElement('h2'); title.textContent = 'Huifa Media Downloader';
      const label = document.createElement('label'); label.className = 'field'; label.textContent = '访问令牌 / Access token';
      const token = document.createElement('input'); token.type = 'password'; token.autocomplete = 'current-password'; token.required = true; token.setAttribute('aria-label', label.textContent);
      label.append(token);
      const error = document.createElement('p'); error.className = 'error-text'; error.setAttribute('role', 'alert');
      const submit = document.createElement('button'); submit.className = 'button primary'; submit.type = 'submit'; submit.textContent = '连接 / Connect';
      form.append(title, label, error, submit); overlay.append(form); document.body.append(overlay); token.focus();
      form.addEventListener('submit', async event => {
        event.preventDefault(); submit.disabled = true;
        try {
          const value = await responseData(await fetch('/api/login', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: token.value }) }));
          csrf = value.csrf; token.value = ''; overlay.remove(); resolve();
        } catch (reason) { error.textContent = reason instanceof Error ? reason.message : String(reason); }
        finally { submit.disabled = false; }
      });
    });
  })();
  async function request(path: string, body: unknown): Promise<any> {
    await ready;
    return responseData(await fetch(path, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: JSON.stringify(body) }));
  }
  function download(url: string): void {
    const parsed = new URL(url, location.href);
    if (parsed.origin !== location.origin || !parsed.pathname.startsWith('/api/')) throw new Error('Invalid result download');
    const anchor = document.createElement('a'); anchor.href = parsed.href; anchor.download = ''; document.body.append(anchor); anchor.click(); anchor.remove();
  }
  ready.then(() => {
    const stream = new EventSource('/api/events');
    stream.onmessage = async message => {
      const event: HostEvent = JSON.parse(message.data);
      if (event.event === 'native') {
        const value = event.data || {};
        if (value.action === 'copy') {
          try { await navigator.clipboard.writeText(String(value.text || '')); }
          catch { dispatch({ event: 'backendError', data: { message: '浏览器未允许复制，请使用安全连接或手动复制。 / Clipboard permission is unavailable.' } }); }
        } else if (value.downloadUrl) download(value.downloadUrl);
        else if (value.action === 'openDirectory') dispatch({ event: 'backendError', data: { message: `服务器目录 / Server folder: ${value.path || ''}` } });
        return;
      }
      dispatch(event);
    };
    stream.onerror = () => { if (stream.readyState === EventSource.CLOSED) dispatch({ event: 'backendDisconnected', data: { message: '服务连接已关闭，请重新载入页面。 / Service connection closed. Reload this page.' } }); };
  }).catch(error => dispatch({ event: 'backendDisconnected', data: { message: error instanceof Error ? error.message : String(error) } }));
  window.huifa = Object.freeze({
    environment: 'web' as const,
    call: async (target: string, action: string, ...args: unknown[]) => { const value = await request('/api/command', { target, action, args }); return value.result; },
    subscribe: (listener: (event: HostEvent) => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; },
    choosePath: async (options: PathOptions): Promise<string> => {
      await ready;
      if (options.kind !== 'file') {
        return await new Promise<string>(resolve => {
          const dialog = document.createElement('dialog'); dialog.className = 'dialog small';
          const form = document.createElement('form'); form.method = 'dialog'; form.className = 'web-path-form';
          const heading = document.createElement('h2'); heading.textContent = options.title || '服务器路径 / Server path';
          const hint = document.createElement('p'); hint.textContent = '输入容器中的路径；下载文件可在任务详情获取。 / Enter a path inside the container. Download results from task details.';
          const path = document.createElement('input'); path.value = options.defaultPath || ''; path.setAttribute('aria-label', 'Server path');
          const buttons = document.createElement('div'); buttons.className = 'actions';
          const cancel = document.createElement('button'); cancel.className = 'button'; cancel.type = 'button'; cancel.textContent = '取消 / Cancel'; cancel.onclick = () => { dialog.close(); };
          const confirm = document.createElement('button'); confirm.className = 'button primary'; confirm.type = 'submit'; confirm.textContent = '确定 / Use path';
          let result = ''; form.onsubmit = () => { result = path.value.trim(); }; dialog.onclose = () => { dialog.remove(); resolve(result); };
          buttons.append(cancel, confirm); form.append(heading, hint, path, buttons); dialog.append(form); document.body.append(dialog); dialog.showModal(); path.focus();
        });
      }
      return new Promise<string>(resolve => {
        const input = document.createElement('input'); input.type = 'file';
        if (options.filters?.length) input.accept = options.filters.flatMap(filter => filter.extensions.map(extension => `.${extension}`)).join(',');
        input.oncancel = () => resolve('');
        input.onchange = async () => {
          const file = input.files?.[0]; if (!file) { resolve(''); return; }
          try {
            const value = await responseData(await fetch('/api/upload', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/octet-stream', 'X-CSRF-Token': csrf, 'X-File-Name': encodeURIComponent(file.name) }, body: file }));
            resolve(value.path);
          } catch (error) { dispatch({ event: 'backendError', data: { message: error instanceof Error ? error.message : String(error) } }); resolve(''); }
        };
        input.click();
      });
    },
    openExternal: async (url: string): Promise<void> => {
      const parsed = new URL(url); if (parsed.protocol !== 'https:' || parsed.username || parsed.password) throw new Error('Only public HTTPS links are allowed');
      window.open(parsed.href, '_blank', 'noopener,noreferrer');
    },
  });
})();
