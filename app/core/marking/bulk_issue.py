"""Последовательное получение нескольких товаров с сохранением частичного итога."""
from . import issue, service


def fetch(credentials, order, requests, contour, thumbprint, inn, progress=None):
    # Проверяем весь план до первого расходного запроса.
    available = {buffer.gtin: buffer.left for buffer in order.buffers}
    seen = set()
    for product, count in requests:
        if product.gtin in seen or not 1 <= count <= min(
                available.get(product.gtin, 0), issue.MAX_PER_REQUEST):
            raise ValueError("Недопустимое количество или повтор GTIN в плане получения.")
        seen.add(product.gtin)
    batches = []
    for index, (product, count) in enumerate(requests):
        try:
            batch, credentials = service.fetch_suz_codes(
                credentials, order.id, product.gtin, count, contour, thumbprint, inn,
                product=product, product_group=order.product_group,
                release_method=str(order.raw.get("releaseMethodType") or ""))
        except Exception as error:  # Частичный результат нельзя терять при любой ошибке выдачи.
            return batches, credentials, f"{product.gtin}: {error}"
        batches.append(batch)
        if progress:
            progress(index + 1, len(requests))
    return batches, credentials, ""
