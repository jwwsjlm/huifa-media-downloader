import { validateVersion } from './rust-release.mjs';

export function renderReleaseNotes(version, notes, assets) {
  if (typeof version !== 'string' || version.trim() !== version) throw new Error('Invalid release version');
  validateVersion(version);
  if (typeof notes !== 'string') throw new Error('Release notes must be text');
  if (!Array.isArray(assets) || assets.some(name => typeof name !== 'string' || name.length > 255 || name.trim() !== name || !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(name))) {
    throw new Error('Invalid release asset filename');
  }
  const available = new Set(assets);
  const prefix = `HuifaDownloader-${version}-`;
  const base = `https://github.com/jwwsjlm/huifa-media-downloader/releases/download/v${version}/`;
  const choices = [
    ['windows-x64-setup.exe', 'Windows x64', '推荐：普通 Intel / AMD 电脑，安装后使用 / Recommended for Intel / AMD PCs; install to use', '安装版 / Installer'],
    ['windows-x64-portable.zip', 'Windows x64', '免安装：普通 Intel / AMD 电脑，解压后运行 / Portable for Intel / AMD PCs; extract and run', '便携版 / Portable'],
    ['windows-arm64-setup.exe', 'Windows ARM64', '推荐：Snapdragon 等 ARM 电脑，安装后使用 / Recommended for Snapdragon and other ARM PCs; install to use', '安装版 / Installer'],
    ['windows-arm64-portable.zip', 'Windows ARM64', '免安装：Snapdragon 等 ARM 电脑，解压后运行 / Portable for Snapdragon and other ARM PCs; extract and run', '便携版 / Portable'],
    ['linux-x64.AppImage', 'Linux x64', 'Intel / AMD 电脑；下载后添加可执行权限 / Intel / AMD PCs; make executable after downloading', 'AppImage'],
    ['linux-arm64.AppImage', 'Linux ARM64', 'ARM64 设备；下载后添加可执行权限 / ARM64 devices; make executable after downloading', 'AppImage'],
    ['macos-arm64.dmg', 'macOS Apple Silicon', 'macOS 15+，Apple M 系列芯片 / macOS 15+, Apple M-series chips', 'Apple Silicon DMG'],
    ['macos-x64.dmg', 'macOS Intel', 'macOS 15+，Intel 芯片的 Mac / macOS 15+, Intel processors', 'Intel DMG'],
  ];
  const rows = choices.filter(([suffix]) => available.has(prefix + suffix)).map(([suffix, platform, advice, label]) => `| ${platform} | ${advice} | [${label}](${base}${prefix}${suffix}) |`);
  const guide = [
    '## 选择下载包 / Choose your download',
    '',
    ...(rows.length ? [
      '| 系统 / Platform | 选择建议 / Choose this if | 直接下载 / Direct download |',
      '| --- | --- | --- |',
      ...rows,
    ] : ['此版本暂无桌面下载包。 / No desktop download packages are available for this release.']),
    '',
    '普通用户只需选择一个对应的安装包或便携包。`.sig`、`.json` 是签名或更新信息，`SHA256SUMS` 用于校验，无需一并下载。',
    '',
    'Most users only need one matching installer or portable package. `.sig` and `.json` contain signatures or update metadata; `SHA256SUMS` is for integrity checks. These extra files are optional.',
  ];
  if (choices.slice(-2).some(([suffix]) => available.has(prefix + suffix))) {
    guide.push('', 'macOS：请从本官方发布页下载。首次打开若系统提示开发者未认证，可在“系统设置 → 隐私与安全”中允许打开。', '', 'macOS: download from this official release page. If macOS reports an unidentified developer on first launch, allow the app under System Settings → Privacy & Security.');
  }
  return guide.join('\n') + '\n\n---\n\n' + notes;
}
