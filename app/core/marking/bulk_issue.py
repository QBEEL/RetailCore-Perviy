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
            attributes = order.raw.get("attributes") or {}
            method = (attributes.get("releaseMethodType")
                      if isinstance(attributes, dict) else "") or order.raw.get("releaseMethodType") or ""
            cis_type = next((str(getattr(buffer, "raw", {}).get("cisType") or "")
                             for buffer in order.buffers if buffer.gtin == product.gtin), "")
            extra = {"cis_type": cis_type} if cis_type else {}
            batch, credentials = service.fetch_suz_codes(
                credentials, order.id, product.gtin, count, contour, thumbprint, inn,
                product=product, product_group=order.product_group,
                release_method=str(method), **extra)
        except Exception as error:  # Частичный результат нельзя терять при любой ошибке выдачи.
            return batches, credentials, f"{product.gtin}: {error}"
        batches.append(batch)
        if progress:
            progress(index + 1, len(requests))
    return batches, credentials, ""
