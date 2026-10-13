import zh from '../../languages/zh-CN.json';
import en from '../../languages/en-US.json';
import type { Locale, Settings } from './types';

export function localeFor(settings: Settings): Locale {
  const value = settings.uiLanguage || settings.locale || 'auto';
  return value === 'auto' ? navigator.language.toLowerCase().startsWith('zh') ? 'zh-CN' : 'en-US' : String(value).startsWith('zh') ? 'zh-CN' : 'en-US';
}
export function translate(locale: Locale, chinese: string, english: string): string {
  const dictionary: Record<string, string> = locale === 'zh-CN' ? zh.translations : en.translations;
  return dictionary[english] || (locale === 'zh-CN' ? chinese : english);
}
export type Translate = (chinese: string, english: string) => string;
