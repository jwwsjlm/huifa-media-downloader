import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { OptionsForm } from './options';

it.each([false, true])('shows the default subtitle fallback for saved options in quick=%s and lets it be disabled', quick => {
  const change = vi.fn();
  const props = { change, quick, t: (_zh: string, en: string) => en };
  const view = render(<OptionsForm {...props} options={{ subtitleLanguage: 'zh-Hans' }} />);
  const checkbox = screen.getByRole('checkbox', { name: 'Continue when subtitles are unavailable' }) as HTMLInputElement;
  expect(checkbox.checked).toBe(true);
  fireEvent.click(checkbox);
  expect(change).toHaveBeenCalledWith('skipUnavailableSubtitles', false);
  view.rerender(<OptionsForm {...props} options={{ subtitleLanguage: 'zh-Hans', skipUnavailableSubtitles: false }} />);
  expect(checkbox.checked).toBe(false);
});
