"""
ken_burns.py — плавный зум неподвижной картинки одной формулой на всех.

Почему не `zoompan`
-------------------
`zoompan` считает точку центра в ЦЕЛЫХ пикселях: `x='iw/2-(iw/zoom/2)'`. Когда
приращение зума за кадр меньше пикселя, координата то стоит на месте, то
прыгает на единицу — картинка дрожит. На мягком фото это незаметно, а на тонких
прямых линиях (стойки, резьба, кромки досок) читается как тряска. Живой случай
26.08: хвост ютуб-клипа из товарного фото сырного пресса заметно дёргался, тогда
как соседний кадр в том же ролике шёл спокойно — он собирался другим способом.

Способ здесь тот же, что в `assemble.py`: размер задаётся ВЫРАЖЕНИЕМ с
`eval=frame`, а кадр вырезается из увеличенного изображения. Дробный масштаб
даёт плавный ход, а `crop` берёт целые пиксели уже из большого кадра, где
единица не видна.

Почему копия, а не импорт из `assemble.py`: тот файл раскатан копиями по
папкам проектов и работает отдельно от `agent/modules` — импортировать его
оттуда нельзя. Формула короткая, поэтому здесь она приведена целиком; менять
надо оба места сразу.
"""
from __future__ import annotations

import os

_ALLOWED_FLAGS = {'bilinear', 'bicubic', 'lanczos', 'fast_bilinear'}


def still_zoom_filter(
    target_w: int,
    target_h: int,
    duration: float,
    *,
    zoom_amount: float = 0.06,
    flags: str = 'bilinear',
    fps: int | None = None,
    supersample: int | None = None,
) -> str:
    """Фильтр ffmpeg: неподвижная картинка → плавный наезд на `duration` секунд.

    Кадр сперва вписывается в целевой размер с обрезкой лишнего, затем растёт от
    100% до `100% + zoom_amount` и вырезается по центру.

    `supersample` (1/2/3, по умолчанию из `ZOOM_SUPERSAMPLE`) — во сколько раз
    считать движение крупнее финального кадра. `scale` выдаёт ЦЕЛОЕ число
    пикселей, а при зуме 6% за 6 секунд картинка должна ползти примерно на
    0.45 px за кадр: целый шаг означает, что через кадр она стоит, а потом
    прыгает. Считая крупнее и ужимая в конце, мы делаем ход субпиксельным —
    дрожание уходит (замер: сейчас 63 неподвижных кадра из 176, при ×2 — ни
    одного). Плата — время склейки: примерно +20%. Умолчание — 2.
    """
    width = max(2, int(target_w))
    height = max(2, int(target_h))
    seconds = max(0.1, float(duration))
    amount = max(0.0, float(zoom_amount))
    resampler = flags if flags in _ALLOWED_FLAGS else 'bilinear'
    if supersample is None:
        try:
            supersample = int(os.getenv('ZOOM_SUPERSAMPLE', '2') or '2')
        except (TypeError, ValueError):
            supersample = 2
    factor = min(3, max(1, int(supersample)))
    work_w, work_h = width * factor, height * factor

    progress = f"({amount} * t/{seconds:.6f})"
    chain = [
        f"scale={width}:{height}:force_original_aspect_ratio=increase",
        f"crop={width}:{height}",
        f"scale=w='{work_w}*(1.0+{progress})':h='{work_h}*(1.0+{progress})':"
        f"eval=frame:flags={resampler}",
        f"crop={work_w}:{work_h}:'(in_w-{work_w})/2':'(in_h-{work_h})/2'",
    ]
    if factor > 1:
        # Ужимаем последним шагом — именно здесь шаг становится субпиксельным.
        chain.append(f"scale={width}:{height}:flags=bicubic")
    if fps:
        chain.append(f"fps={int(fps)}")
    chain.append("format=yuv420p")
    return ",".join(chain)
