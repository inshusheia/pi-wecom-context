declare module "bun:test" {
  export function afterEach(callback: () => unknown | Promise<unknown>): void;
  export function describe(name: string, callback: () => unknown): void;
  export function expect(value: unknown): any;
  export function test(name: string, callback: () => unknown | Promise<unknown>): void;
}

interface ImportMeta {
  readonly dir: string;
}
