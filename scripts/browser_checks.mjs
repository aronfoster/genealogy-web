import AxeBuilder from "@axe-core/playwright";
import { mkdir } from "node:fs/promises";

// axe still evaluates every selected rule on every node/frame. We assert only
// violations; avoid constructing and transferring every passing-node selector.
export function accessibility(page) {
  return new AxeBuilder({ page }).options({ resultTypes: ["violations"] });
}

// These images are review aids, not pixel assertions. Keep the prototype's
// manifest/hash comparisons in check.mjs independent of this diagnostic policy.
export async function screenshot(page, options) {
  if (process.env.BROWSER_SCREENSHOTS === "all") await page.screenshot(options);
}

export async function failureScreenshots(browser, directory) {
  // Never mask the test failure if a crashed/closed page cannot be captured.
  try {
    await mkdir(directory, { recursive: true });
    let index = 0;
    for (const context of browser.contexts()) {
      for (const page of context.pages()) {
        if (page.url() === "about:blank") continue;
        const path = `${directory}/failure-${index++}.png`;
        try {
          await page.screenshot({ path, fullPage: true, timeout: 5000 });
        } catch (error) {
          console.error(`Could not capture ${path}: ${error.message}`);
        }
      }
    }
  } catch (error) {
    console.error(`Could not create failure diagnostics: ${error.message}`);
  }
}
