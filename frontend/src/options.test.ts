import { describe, expect, it } from 'vitest';
import { commandOptions, defaults, optionGroups } from './options';

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
});
