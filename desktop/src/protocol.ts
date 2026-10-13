export const MAX_REQUEST_BYTES = 1024 * 1024;
export const MAX_RESPONSE_BYTES = 16 * 1024 * 1024;
export const MAX_PENDING = 128;

export function validateCommand(value: unknown): { target: string; action: string; args: unknown[] } {
  if (!value || typeof value !== 'object') throw new Error('Invalid command');
  const command = value as Record<string, unknown>;
  if (Object.keys(command).sort().join(',') !== 'action,args,target' || typeof command.target !== 'string' || typeof command.action !== 'string' || !Array.isArray(command.args)) throw new Error('Invalid command');
  if (!/^[a-z]+$/.test(command.target) || !/^[A-Za-z]+$/.test(command.action) || command.args.length > 4) throw new Error('Invalid command');
  if (Buffer.byteLength(JSON.stringify(command)) > MAX_REQUEST_BYTES - 100) throw new Error('Command is too large');
  return command as { target: string; action: string; args: unknown[] };
}

export function externalUrl(value: unknown): string {
  if (typeof value !== 'string' || value.length > 4096) throw new Error('Invalid URL');
  const url = new URL(value);
  if (url.protocol !== 'https:' || url.username || url.password) throw new Error('Only HTTPS links are allowed');
  return url.href;
}
export function lifecycleArguments(args: string[]): string[] {
  return args.flatMap((argument, index) => argument.startsWith('--veloapp-') ? [argument, ...(args[index + 1] && !args[index + 1].startsWith('--') ? [args[index + 1]] : [])] : []);
}
