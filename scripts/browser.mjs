export async function openArtifact(page, base, item) {
  await page.setViewportSize(item.viewport);
  await page.goto(base + item.route);
  await page.evaluate(async () => {
    await Promise.all([
      document.fonts.load('400 16px "Archive Sans"'),
      document.fonts.load('700 16px "Archive Sans"'),
      document.fonts.load('400 16px "Archive Serif"'),
      document.fonts.load('italic 400 16px "Archive Serif"'),
    ]);
    await document.fonts.ready;
  });
  if (item.expire) {
    await page.getByRole("button", { name: "Simulate session expiry" }).click();
    await page.waitForURL("**/?view=access&state=revoked");
  }
  // Review-only controls are outside the proposed reader experience.
  if (item.element)
    await page.addStyleTag({
      content: ".site-header { position:static !important; }",
    });
  await page.addStyleTag({
    content: ".review-controls { display:none !important; }",
  });
}
