export function downloadLinks(text: string): string[] {
  const matches = text.match(/https?:\/\/[^\s<>"“”]+/gi) || [];
  const urls = matches.map(value => {
    const url = new URL(value.replace(/[，。；！、）\]),;.!]+$/g, ''));
    if (url.username || url.password) throw new Error('Enter HTTP / HTTPS URLs without login credentials');
    return url.href;
  });
  const distinct = [...new Set(urls)];
  if (!distinct.length) throw new Error('No HTTP / HTTPS download links found');
  if (distinct.length > 1000) throw new Error('Add at most 1000 URLs at once');
  return distinct;
}

export function singleVideoLink(value: string): boolean {
  const url = new URL(value); const host = url.hostname.toLowerCase();
  if (host === 'youtu.be') return url.pathname.length > 1;
  if (host === 'youtube.com' || host.endsWith('.youtube.com')) return url.pathname === '/watch' && Boolean(url.searchParams.get('v')) || /^\/(shorts|live|embed)\/[^/]+/.test(url.pathname);
  if (host === 'bilibili.com' || host.endsWith('.bilibili.com')) return /^\/video\/(BV[a-z\d]+|av\d+)/i.test(url.pathname);
  return /\.(mp4|webm|mkv|mov|mp3|m4a|flac|wav|ogg|opus)$/i.test(url.pathname);
}
