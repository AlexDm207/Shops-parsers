import sys
import traceback

sys.path.insert(0, r"c:\Users\79295\Desktop\Git_work\shops-parsers")

checks = [
    ("magnit", lambda: __import__("food.magnit.scraper_magnit", fromlist=["MagnitScraper"]).MagnitScraper(max_pages_per_category=1, delay=0.5, max_retries=1).run()),
    ("pyaterochka", lambda: __import__("food.pyaterochka.scraper_pyaterochka", fromlist=["PyaterochkaScraper", "CATEGORY_URLS"]).PyaterochkaScraper(category_urls=__import__("food.pyaterochka.scraper_pyaterochka", fromlist=["CATEGORY_URLS"]).CATEGORY_URLS[:1], max_pages_per_category=1, delay=0.5, max_retries=0).run()),
    ("mvideo", lambda: __import__("electronics.M_video.scraper_mvideo", fromlist=["MVideoScraper", "ACTION_URLS"]).MVideoScraper(action_urls=__import__("electronics.M_video.scraper_mvideo", fromlist=["ACTION_URLS"]).ACTION_URLS[:1], max_pages_per_action=1, delay=0.5, max_retries=1).run()),
    ("holodilnik", lambda: __import__("electronics.holodilnik.scraper_holodilnik", fromlist=["HolodilnikScraper", "ACTION_URLS"]).HolodilnikScraper(action_urls=__import__("electronics.holodilnik.scraper_holodilnik", fromlist=["ACTION_URLS"]).ACTION_URLS[:1], max_pages_per_action=1, delay=0.5, max_retries=1).run()),
    ("citilink", lambda: __import__("electronics.citilink.scraper_citilink", fromlist=["CitilinkScraper"]).CitilinkScraper(max_action_pages=1, min_delay=0.5, max_delay=0.5, max_retries=1).run()),
    ("iledebeaute", lambda: __import__("cosmetics.ildebote.scraper_iledebeaute", fromlist=["IleDeBeauteScraper"]).IleDeBeauteScraper(max_pages=1, delay=0.5).run()),
    ("podruzhka", lambda: __import__("cosmetics.podrushca.scraper_podrygka", fromlist=["PodrygkaScraper"]).PodrygkaScraper("https://www.podrygka.ru/catalog/?page=1", max_pages=1, min_delay=0.5, max_delay=0.5).run()),
    ("rivegosh", lambda: __import__("cosmetics.rivgosh.scraper_rivegauche", fromlist=["RiveGaucheScraper"]).RiveGaucheScraper("https://rivegauche.ru/tags/sale?currentPage=1", max_pages=1, delay=0.5).run()),
]

for name, run in checks:
    try:
        products = run()
        valid = all({"shop", "source_url", "source_product_id", "name", "current_price_text", "old_price_text", "discount_text", "in_stock", "collected_at"} <= item.keys() for item in products)
        print(f"{name}: products={len(products)} valid_raw={valid}", flush=True)
    except Exception as error:
        print(f"{name}: ERROR {type(error).__name__}: {error}", flush=True)
        traceback.print_exc()
