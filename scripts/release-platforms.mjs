export const releasePlatforms = {
  'windows-x86_64': { suffix: 'windows-x64', bundle: 'nsis', extension: '.exe' },
  'windows-aarch64': { suffix: 'windows-arm64', bundle: 'nsis', extension: '.exe' },
  'linux-x86_64': { suffix: 'linux-x64', bundle: 'appimage', extension: '.AppImage' },
  'linux-aarch64': { suffix: 'linux-arm64', bundle: 'appimage', extension: '.AppImage' },
  'darwin-x86_64': { suffix: 'macos-x64', bundle: 'macos', extension: '.app.tar.gz' },
  'darwin-aarch64': { suffix: 'macos-arm64', bundle: 'macos', extension: '.app.tar.gz' },
};

export const isReleaseAsset = name => /\.(exe|AppImage|zip|dmg|tar\.gz|sig|json)$/.test(name) || name === 'SHA256SUMS';
