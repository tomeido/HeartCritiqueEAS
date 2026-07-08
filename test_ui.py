import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        import os
        pwd = os.getcwd()

        await page.goto(f"file://{pwd}/static/index.html")

        # Mock loadStoryList directly in page context to add an artificial delay and avoid real network
        await page.evaluate("""
            window.originalLoadStoryList = window.loadStoryList;
            window.loadStoryList = async function() {
                await new Promise(resolve => setTimeout(resolve, 500));
            };
            // Also need to mock updateSearchStatus to show the clear button
            window.originalUpdateSearchStatus = window.updateSearchStatus;
            window.updateSearchStatus = function() {
                const clearBtn = document.getElementById('story-search-clear');
                if (clearBtn) clearBtn.hidden = false;
            };
        """)

        # Press a key to trigger the debounced keydown
        await page.focus("#story-search")
        await page.keyboard.type("t")

        # Debounce takes 300ms, wait for it
        await asyncio.sleep(0.4)

        # Wait for the spinner class to be added
        await page.wait_for_selector(".list-search-icon.spin")

        # Ensure the icon changes back
        await page.wait_for_selector(".list-search-icon:not(.spin)", timeout=5000)

        print("Spinner appeared and disappeared properly on input.")

        # Force clear button to be visible since we mocked the search update
        await page.evaluate("document.getElementById('story-search-clear').hidden = false")

        # Check clear button as well
        await page.click("#story-search-clear")
        await page.wait_for_selector(".list-search-icon.spin")
        await page.wait_for_selector(".list-search-icon:not(.spin)", timeout=5000)
        print("Spinner appeared and disappeared properly on clear.")

        await browser.close()

asyncio.run(main())
