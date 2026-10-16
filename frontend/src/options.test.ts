import { describe, expect, it } from 'vitest';
import { automaticQuality, commandOptions, defaults, optionGroups, optionLabel } from './options';

describe('download option boundary', () => {
  it('removes a public secret marker while preserving omitted and explicitly cleared proxies', () => {
    const saved = { ...defaults, proxyConfigured: true };
    expect(commandOptions(saved)).not.toHaveProperty('proxy');
    expect(commandOptions(saved)).not.toHaveProperty('proxyConfigured');
    expect(commandOptions({ ...saved, proxy: '' })).toHaveProperty('proxy', '');
    expect(saved.proxyConfigured).toBe(true);
  });
  it('uses only frozen core option field names in the full form', () => {
    const fields = optionGroups.flatMap(group => group.fields.map(field => field.key));
    expect(new Set(fields).size).toBe(fields.length);
    expect(fields).toContain('prependCoverEnabled'); expect(fields).toContain('automaticSubtitles');
    expect(fields).not.toContain('prependCover'); expect(fields).not.toContain('maxConcurrent');
    expect(commandOptions(defaults).prependCoverFrames).toBe(3);
  });
  it('clears exact formats when choosing automatic quality and preserves audio-only mode', () => {
    const saved = { ...defaults, contentMode: 'manual', formatId: 'old', videoFormatId: 'old-video', audioFormatIds: ['old-audio'] };
    expect(automaticQuality(saved, 'best')).toEqual({ quality: 'best', contentMode: 'video', formatId: '', videoFormatId: '', audioFormatIds: [] });
    expect(automaticQuality({ ...saved, contentMode: 'audio' }, 'worst').contentMode).toBe('audio');
    expect(saved.formatId).toBe('old');
  });
  it('shows readable choice labels without changing submitted ids', () => {
    const t = (zh: string, en: string) => en;
    expect(optionLabel('best', t)).toBe('Best match');
    expect(optionLabel('auto', t)).toBe('Automatic');
    const field = optionGroups.flatMap(group => group.fields).find(value => value.key === 'compatibilityTarget');
    expect(field?.choices?.find(([id]) => id === 'windows')?.[0]).toBe('windows');
    expect(field?.choices?.find(([id]) => id === 'windows')?.[2]).toBe('Windows');
  });
});
