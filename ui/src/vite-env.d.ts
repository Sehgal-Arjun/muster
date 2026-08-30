/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Where `muster serve` is listening. */
  readonly VITE_MUSTER_API?: string;
  /** "1" runs the whole screen against src/mock.ts with no server. */
  readonly VITE_USE_MOCK?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
