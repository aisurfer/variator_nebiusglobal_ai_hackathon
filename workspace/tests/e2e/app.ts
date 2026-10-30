// The app lives under /workspace/ (WP27): the base URL of the tests is <origin>/workspace/ (playwright.config.ts).
// page.goto('/brief') would drop the base; app('/brief') is the same in-app path relative to the base URL ('brief').
export const app = (path: string) => path.replace(/^\/+/, '') || './';
