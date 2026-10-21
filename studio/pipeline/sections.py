"""Разделы вкладки «Конвейер»: какие параметры, как подписаны, когда видны.

Функция sections(page) возвращает список разделов. Нестандартные элементы
(пресеты, квота GPT, выбор голоса, ключи VeoNonStop и т.п.)
создаются методами страницы — здесь на них только ссылки через Custom.
"""
from __future__ import annotations

from .. import catalog
from ..i18n import T
from ..ui.form import Custom, F, Group, Note, Row
from .defaults import parallax_supported, slideshow, video_off

SEC = T("сек", "sec")
MIN = T("мин", "min")

# Подсказки к моделям, общие для «Генерации картинок» и «Инфографики»
VERTEX_MODEL_TIP = T(
    "• Banana Pro — самая качественная.\n"
    "• Banana 2 — баланс качества и скорости.\n"
    "• Banana 2 Lite — быстрее и дешевле, кадр 1K.\n"
    "Картинки строятся только по тексту. Ключ и PROJECT_ID — в «Настройках».",
    "• Banana Pro — best quality.\n"
    "• Banana 2 — balance of quality and speed.\n"
    "• Banana 2 Lite — faster and cheaper, 1K frames.\n"
    "Images are made from text only. Key and PROJECT_ID are in Settings.")
CHEAPVIBE_MODEL_TIP = T(
    "• GPT Image 2 — модель OpenAI (gpt-image-2).\n"
    "• Grok Imagine — модель xAI (grok-imagine-image).\n"
    "• Banana 2 — модель Google (nano-banana-2), качество только low.\n"
    "Списание у моделей разное — оно показано под качеством.\n"
    "Ключ один на все — в «Настройках» → CheapVibeCode.",
    "• GPT Image 2 — the OpenAI model (gpt-image-2).\n"
    "• Grok Imagine — the xAI model (grok-imagine-image).\n"
    "• Banana 2 — the Google model (nano-banana-2), low quality only.\n"
    "The models charge differently — the cost is shown under quality.\n"
    "One key for all — Settings → CheapVibeCode.")
VARAG_IMAGE_NOTE = T(
    "Модель выбирает сам сервис, картинки строятся только по тексту. Ключ mk_… — в «Настройках» → VARAG.",
    "The service picks the model; images are made from text only. The mk_… key is in Settings → VARAG.")
VARAG_PARALLEL_TIP = T(
    "Сколько задач держать одновременно. Потолок — слоты вашего тарифа VARAG: больше не пойдёт,\n"
    "лишнее подождёт своей очереди. На тарифе chain картинки и видео делят одни слоты.",
    "How many jobs to keep at once. Capped by your VARAG plan's slots: more won't run, the extra\n"
    "waits its turn. On the chain plan images and video share the same slots.")


FRAME_FORMAT_TIP = T(
    "Формат кадра всего ролика: 16:9 — горизонталь (YouTube), 9:16 — вертикаль (Shorts/Reels/TikTok).\n"
    "Он один для картинок, инфографики, ремиксера, видео и склейки — меняется во всех блоках\n"
    "сразу. Каждая модель получает формат строкой в конце промпта, а модели, которые понимают\n"
    "параметр формата, — ещё и в запросе.",
    "Frame format of the whole video: 16:9 — horizontal (YouTube), 9:16 — vertical (Shorts/Reels/TikTok).\n"
    "It is shared by images, infographics, the remixer, video and the render — it changes in every\n"
    "block at once. Every model gets the format as the last line of the prompt, and models that\n"
    "understand a format parameter also get it in the request.")


def _frame_format(visible=None):
    """«Формат кадра» — одно значение (veo_ratio), показанное в каждом блоке генерации."""
    return F("veo_ratio", "seg", T("Формат кадра", "Frame format"), FRAME_FORMAT_TIP,
             options=catalog.FRAME_FORMATS, visible=visible)


def _models(provider_key, table=catalog.OPERATOR_MODELS):
    """Модели выбранного оператора для кнопок: у Codex на кнопке имя модели, значение — slug."""
    def fn(v):
        return [(m, catalog.CODEX_MODEL_NAMES.get(m, m)) for m in table.get(v.get(provider_key) or "", [])]
    return fn


def sections(page) -> list[dict]:
    """Список разделов: {key, title, icon, items, tip}."""
    return [
        _preset(page), _start(page), _direction(page), _intro(page),
        _stock(page), _infographics(page), _motion(page), _images(page), _remixer(page), _video(page),
        _mix(page), _render(page),
    ]


# =============================================================== Пресет
def _preset(page):
    return {
        "key": "preset", "icon": "◈", "title": T("Пресет", "Preset"),
        "tip": T("Наборы настроек всего конвейера. Выберите — применятся разом.",
                 "Saved sets of all pipeline settings. Pick one to apply everything at once."),
        "items": [Custom(page.build_preset_bar)],
    }


# ======================================================== Точка старта
def _start(page):
    return {
        "key": "start", "icon": "▶", "title": T("Точка старта и квота GPT", "Start point & GPT quota"),
        "items": [
            F("start_point", "seg", T("С чего начинать", "Start from"),
              T("С чего начинать конвейер:\n"
                "• Из аудио — есть готовая озвучка (mp3/wav): её расшифруют и смонтируют.",
                "Where the pipeline starts:\n"
                "• From audio — you have a finished voice-over (mp3/wav): it is transcribed and edited."),
              options=[("audio", T("Из аудио", "From audio"))]),
            Custom(page.build_quota_panel),
        ],
    }


# ============================================ Транскрипция и режиссура
def _direction(page):
    return {
        "key": "direction", "icon": "✎", "title": T("Транскрипция и режиссура", "Transcription & direction"),
        "items": [
            F("language", "combo", T("Язык озвучки", "Voice language"),
              T("Язык озвучки — для распознавания речи (AssemblyAI).\n"
                "Укажите язык аудио, иначе слова расшифруются с ошибками.",
                "Voice-over language for speech recognition (AssemblyAI).\n"
                "Set the audio language, otherwise words are transcribed with errors."),
              options=catalog.LANGUAGES),
            Row(Custom(page.build_director_badge), label=T("Режиссёр", "Director"),
                tip=T("Кто работает режиссёром: строит монтажный план (разбивку на сцены) и описывает\n"
                      "каждую сцену промптом для генерации. В BegoFrame режиссёр — OpenAI через Codex\n"
                      "(по подписке ChatGPT, токены не тарифицируются). Вход — в «Настройках».",
                      "Who acts as the director: builds the montage plan (scene breakdown) and writes a\n"
                      "generation prompt for every scene. In BegoFrame the director is OpenAI via Codex\n"
                      "(ChatGPT subscription, tokens are not billed). Sign in under Settings.")),
            F("gpt_model", "seg", T("Модель", "Model"),
              T("Список берётся у самого Codex — показаны все модели, доступные вашему аккаунту.\n"
                "• Astra — самая мощная: сложные длинные планы.\n"
                "• Sol — основная рабочая модель (новее — выше номер версии).\n"
                "• Terra — сбалансированная: повседневная работа, большие объёмы.\n"
                "• Luna — быстрая и дешёвая: рутина и черновики.\n"
                "• Версии 5.x — прежние поколения.\n"
                "Через Codex токены не тарифицируются (подписка).",
                "The list comes from Codex itself — every model available to your account is shown.\n"
                "• Astra — the most capable: complex long plans.\n"
                "• Sol — the main workhorse (higher version number is newer).\n"
                "• Terra — balanced: everyday work, large volumes.\n"
                "• Luna — fast and cheap: routine and drafts.\n"
                "• 5.x versions — older generations.\n"
                "Through Codex tokens are not billed (subscription)."),
              options=lambda v: catalog.codex_options()),
            F("direction_mode", "seg", T("Проходы режиссуры", "Direction passes"),
              T("Сколько заходов к модели делает режиссура.\n"
                "1 проход: план сцен и промпты — за один запрос (быстрее).\n"
                "2 прохода: сначала план сцен, потом отдельно промпты.\n"
                "2 прохода надёжнее для длинных роликов (200+ сцен) — меньше риск обрезки.",
                "How many requests the direction makes.\n"
                "1 pass: scene plan and prompts in one request (faster).\n"
                "2 passes: scene plan first, prompts in a separate request.\n"
                "2 passes are safer for long videos (200+ scenes) — less risk of truncation."),
              options=[("1-pass", T("1 проход", "1 pass")), ("2-pass", T("2 прохода", "2 passes"))]),
            Row(F("pass2_chunk_enabled", "bool", T("Промптаж пачками", "Prompt in batches"),
                  T("Резать описание сцен промптами на пачки: длинный список промптов не влезает в один\n"
                    "ответ модели и обрезается. Выключено — все сцены одним запросом.",
                    "Split prompt writing into batches: a long prompt list does not fit into one\n"
                    "model answer and gets truncated. Off — all scenes in one request.")),
                F("gpt_chunk_size", "int", T("по", "of"),
                  T("Сколько сцен описывать промптами за один запрос.\n"
                    "Больше — меньше запросов, но выше риск обрезки.",
                    "How many scenes per prompt request.\nMore means fewer requests but higher truncation risk."),
                  lo=50, hi=1000, step=50, suffix=T("сцен", "scenes"),
                  enabled=lambda v: v.get("pass2_chunk_enabled")),
                F("gpt_parallel", "int", T("параллельно", "parallel"),
                  T("Сколько пачек промптировать одновременно. 1 — по очереди.",
                    "How many batches run at once. 1 — one after another."),
                  lo=1, hi=20, enabled=lambda v: v.get("pass2_chunk_enabled"))),
            F("clip_mode", "seg", T("Тип клипов", "Clip mode"),
              T("Из чего собирать ролик:\n"
                "• Видео — каждая сцена оживает в видео (дороже, живее).\n"
                "• Слайдшоу — статичные картинки с плавным зумом (дёшево).\n"
                "• Вислайд — смесь: часть сцен видео, остальные слайды.",
                "What the video is made of:\n"
                "• Video — every scene is animated (costlier, livelier).\n"
                "• Slideshow — still images with a smooth zoom (cheap).\n"
                "• Vislide — a mix: some scenes are video, others are slides."),
              options=[("video", T("Видео", "Video")), ("slideshow", T("Слайдшоу", "Slideshow")),
                       ("vislide", T("Вислайд", "Vislide"))]),
            F("vislide_interval", "int", T("Видео каждая", "Video every"),
              T("Как часто в режиме «вислайд» вставлять живое видео.\n"
                "3 = каждая третья сцена — видео, остальные — слайды.",
                "How often vislide inserts a live video.\n3 = every third scene is video, the rest are slides."),
              lo=2, hi=20, suffix=T("-я сцена", "th scene"),
              visible=lambda v: v.get("clip_mode") == "vislide"),
            Row(F("clip_min", "int", T("от", "from"),
                  T("Минимальная длина одной сцены (сек). Короче — динамичнее нарезка.",
                    "Minimum scene length (sec). Shorter means a more dynamic cut."), lo=1, hi=30),
                F("clip_max", "int", T("до", "to"),
                  T("Максимальная длина одной сцены (сек). Длиннее — спокойнее ролик.",
                    "Maximum scene length (sec). Longer means a calmer video."), lo=2, hi=60, suffix=SEC),
                label=T("Длина сцен", "Scene length")),
        ],
    }


# ================================================================ Интро
def _intro(page):
    return {
        "key": "intro", "icon": "✦", "title": T("Вступление", "Intro block"),
        "items": [
            F("intro_duration", "int", T("Зона интро", "Intro zone"),
              T("Длина вступительного блока ролика (сек).\n"
                "В этой зоне сцены нарезаются чаще и динамичнее — цепляют зрителя в первые секунды.\n"
                "0 = без особого интро.",
                "Length of the intro block (sec).\n"
                "Scenes in this zone are cut faster to hook the viewer in the first seconds.\n"
                "0 = no special intro."),
              lo=0, hi=9999, suffix=T("сек (0 = выкл)", "sec (0 = off)")),
            Row(F("intro_clip_min", "int", T("от", "from"),
                  T("Минимальная длина сцены во вступлении. 0 = как у остального ролика.",
                    "Minimum scene length in the intro. 0 = same as the rest."), lo=0, hi=30,
                  enabled=lambda v: v.get("intro_duration", 0) > 0),
                F("intro_clip_max", "int", T("до", "to"),
                  T("Максимальная длина сцены во вступлении. 0 = как у остального ролика.",
                    "Maximum scene length in the intro. 0 = same as the rest."), lo=0, hi=60,
                  suffix=T("сек (0 = как везде)", "sec (0 = inherit)"),
                  enabled=lambda v: v.get("intro_duration", 0) > 0),
                label=T("Сцены интро", "Intro scenes")),
            Row(F("cover_hook_sec", "float", None,
                  T("Сколько секунд длится ожившая обложка в начале ролика.\n"
                    "Длину задаёте вы, а не режиссёр. 0 = не оживлять обложку.\n"
                    "Сама обложка прикладывается к файлу в таблице очереди.",
                    "How many seconds the animated cover lasts at the start.\n"
                    "You set the length, not the director. 0 = don't animate the cover.\n"
                    "The cover itself is attached to a file in the queue table."),
                  lo=0, hi=15, step=0.5, suffix=SEC),
                F("cover_hook_mode", "combo", None,
                  T("Чем оживлять обложку:\n• zoom_in — медленный наезд\n• zoom_out — отъезд\n"
                    "• drift — камера скользит вбок\n• fly_in — залёт внутрь сцены\n"
                    "• awaken — картинка «дышит»: волосы, пар, вода\n"
                    "Тексты правятся во вкладке «Промпты».",
                    "How to animate the cover:\n• zoom_in — slow push in\n• zoom_out — pull out\n"
                    "• drift — the camera slides sideways\n• fly_in — flying into the scene\n"
                    "• awaken — the picture 'breathes': hair, steam, water\n"
                    "Texts are edited on the Prompts page."),
                  options=["zoom_in", "zoom_out", "drift", "fly_in", "awaken"],
                  enabled=lambda v: v.get("cover_hook_sec", 0) > 0),
                label=T("Оживление обложки", "Cover animation")),
        ],
    }


# ================================================================ Сток
def _stock_on(v):
    return bool(v.get("stock_enabled"))


def _stock(page):
    return {
        "key": "stock", "icon": "▦", "title": T("Сток (видео + фото)", "Stock (video + photo)"),
        "items": [
            F("stock_enabled", "bool", T("Стоковые футажи вместо нейрогенерации", "Stock footage instead of AI generation"),
              T("Реальный видео/фото-футаж со стоков вместо нейрогенерации. Режиссёр сам помечает\n"
                "подходящие клипы.\nРиски: Content ID (чаще всего по звуку — поэтому звук убирается),\n"
                "сток — это современная постановка (для истории не подходит), тот же футаж могли\n"
                "использовать другие. Не нашлось → клип уходит в обычную нейрогенерацию.",
                "Real stock video/photo footage instead of AI generation. The director marks suitable\n"
                "clips.\nRisks: Content ID (mostly via audio — that is why audio is muted), stock is\n"
                "modern staged footage (not for history topics), others may use the same clips.\n"
                "Nothing found → the clip goes to regular AI generation.")),
            Group(None, [
                Row(F("stock_src_local", "bool", T("Своя папка", "Own folder"),
                      T("Ваши снятые футажи. Уникальны — при равной релевантности идут первыми.",
                        "Your own footage. Unique — goes first at equal relevance.")),
                    F("stock_src_pexels", "bool", "Pexels",
                      T("Видео и фото. Бесплатный ключ, лимит 25 000/мес. Без ключа — через данные\n"
                        "сайта («Временный API-ключ» в Настройках → Сток, или программа возьмёт его сама).",
                        "Videos and photos. Free key, 25,000/month limit. Without a key — via the site\n"
                        "data (Temporary API key in Settings → Stock, or the app fetches it itself).")),
                    F("stock_src_pixabay", "bool", "Pixabay",
                      T("Бесплатный ключ, 100 запросов/мин. Помечает ролики, созданные ИИ.",
                        "Free key, 100 requests/min. Flags AI-generated videos.")),
                    label=T("Источники", "Sources")),
                F("stock_library_dir", "dir", T("Папка футажей", "Footage folder"),
                  T("Папка с вашими футажами (с подпапками). Подбор идёт по имени файла и папки —\n"
                    "называйте осмысленно: storm/lightning_night.mp4.",
                    "Folder with your footage (with subfolders). Matching uses file and folder names —\n"
                    "name them meaningfully: storm/lightning_night.mp4."),
                  visible=lambda v: v.get("stock_src_local")),
                F("stock_gate_sec", "int", T("Сток с секунды", "Stock from second"),
                  T("С какой секунды ролика стоку разрешено появляться. Вступление — хук. 0 = без ограничения.\n"
                    "Доли сток-видео и сток-фото задаются в разделе «Состав видео».",
                    "From which second stock may appear. The intro is the hook. 0 = no limit.\n"
                    "Stock video and photo shares are set in the 'Video mix' section."),
                  lo=0, hi=300, step=5),
                Row(F("stock_parallel", "int", T("Параллель поиска", "Search parallel"),
                      T("Сколько клипов сток обрабатывает одновременно.", "How many clips stock handles at once."),
                      lo=1, hi=32),
                    F("stock_tail_wait", "int", T("ждать сток", "wait for stock"),
                      T("Сколько ждать сток после готовности остальных стадий, прежде чем отдать остаток\n"
                        "нейрогенерации. 0 — не ждать.",
                        "How long to wait for stock after other stages finish before handing the rest\n"
                        "to AI generation. 0 — don't wait."), lo=0, hi=1800, step=50, suffix=SEC)),
                Row(F("stock_vision_candidates", "int", T("Вижн-кандидатов", "Vision candidates"),
                      T("Сколько футажей показать вижну (по 3 кадра каждого). 0 — брать первый результат.",
                        "How many clips to show to vision (3 frames each). 0 — take the first result."),
                      lo=0, hi=12),
                    F("stock_vision_per_source", "int", T("от каждого стока", "per stock source"),
                      T("Сколько кандидатов гарантированно берётся от каждого включённого стока. 0 — без квоты.",
                        "How many candidates are guaranteed from each enabled stock. 0 — no quota."),
                      lo=0, hi=6, enabled=lambda v: v.get("stock_vision_candidates", 0) > 0)),
                F("stock_vision_model", "seg", T("Вижн (Codex)", "Vision (Codex)"),
                  T("Модель Codex, которая смотрит кадры футажей и выбирает подходящий.",
                    "The Codex model that looks at footage frames and picks the right one."),
                  options=_models("stock_vision_provider", catalog.VISION_MODELS),
                  enabled=lambda v: v.get("stock_vision_candidates", 0) > 0),
                Row(F("stock_start_sec", "float", T("Старт футажа", "Clip start"),
                      T("С какой секунды резать футаж. Первые секунды часто самые постановочные.",
                        "From which second to cut the footage. The first seconds are often staged."),
                      lo=0, hi=60, step=0.5, suffix=SEC),
                    F("stock_jitter", "bool", T("разброс ±", "jitter ±"),
                      T("Случайный разброс старта: у похожих футажей не будет одинаковых первых кадров.",
                        "Random start offset so similar clips don't share first frames.")),
                    F("stock_jitter_sec", "float", None, None, lo=0, hi=30, step=0.5, suffix=SEC,
                      enabled=lambda v: v.get("stock_jitter"))),
                F("stock_wait_timeout", "int", T("Ждать загрузку", "Download wait"),
                  T("Сколько максимум ждать фоновую загрузку стока. Недокачанное догенерирует нейросеть.",
                    "Maximum wait for background stock downloads. Unfinished clips are generated by AI."),
                  lo=1, hi=180, suffix=MIN),
                F("stock_prompt_provider", "seg", T("Оператор запросов", "Query operator"),
                  T("Кто пишет поисковые запросы для стока и подбирает файлы из вашей папки.\n"
                    "«Авто» — тот же, кто режиссирует. Недоступен → запросом станет текст озвучки.",
                    "Who writes stock search queries and matches files from your folder.\n"
                    "'Auto' — same as the director. Unavailable → the voice-over text is used."),
                  options=catalog.STOCK_PROMPT_PROVIDERS),
                F("stock_prompt_model", "seg", T("Модель запросов", "Query model"), None,
                  options=_models("stock_prompt_provider"),
                  visible=lambda v: v.get("stock_prompt_provider") != "auto"),
                Row(F("stock_mute_audio", "bool", T("Убирать звук футажа", "Mute clip audio"),
                      T("Вырезает звук скачанного футажа: музыка ляжет поверх голоса, а звук — главный\n"
                        "канал Content ID-претензий. Держите включённым.",
                        "Removes the clip audio: music would cover the voice, and audio is the main\n"
                        "Content ID channel. Keep it on.")),
                    F("stock_exclude_ai", "bool", T("Без ИИ-роликов", "Exclude AI clips"),
                      T("Отсекать ролики, сгенерированные нейросетью (Pixabay помечает такие файлы).",
                        "Skip AI-generated clips (Pixabay flags such files)."))),
            ], visible=_stock_on),
        ],
    }


# ========================================================= Инфографика
def _infographics(page):
    return {
        "key": "infographics", "icon": "◧", "title": T("Инфографика", "Infographics"),
        "items": [
            F("infographic_enabled", "bool", T("Вставлять ИИ-инфографику", "Insert AI infographics"),
              T("Режиссёр может отвести подходящие поясняющие клипы под инфографику. Такие клипы\n"
                "рисует провайдер ниже, они не идут в видеогенерацию и остаются кадрами.",
                "The director may reserve explanatory clips for an infographic. Those clips are drawn\n"
                "by the provider below, skip video generation and stay as stills.")),
            Group(None, [
                F("infographic_gate_sec", "int", T("Инфографика с секунды", "Infographics from second"),
                  T("С какой секунды допустима инфографика: диаграмма во вступлении убивает хук.\n"
                    "Доля инфографики задаётся в разделе «Состав видео».",
                    "From which second infographics are allowed: a chart in the intro kills the hook.\n"
                    "The infographics share is set in the 'Video mix' section."),
                  lo=0, hi=300, step=5),
                F("infographic_provider", "seg", T("Провайдер", "Provider"),
                  T("Инфографика может рисоваться другим провайдером, чем обычные сцены. Провайдеры и\n"
                    "модели — те же, что в «Генерации картинок»; ключи общие, из «Настроек».",
                    "Infographics may use a different provider than regular scenes. Providers and models\n"
                    "are the same as in 'Image generation'; keys are shared, from Settings."),
                  options=catalog.IMAGE_PROVIDERS),
                _frame_format(),
                F("infographic_veo_model", "seg", T("Модель", "Model"), None,
                  options=catalog.BANANA_MODELS, visible=lambda v: v.get("infographic_provider") == "veononstop"),
                F("infographic_vertex_model", "seg", T("Модель", "Model"), VERTEX_MODEL_TIP,
                  options=catalog.VERTEX_IMAGE_MODELS, visible=lambda v: v.get("infographic_provider") == "vertex"),
                Group(None, [
                    F("infographic_cheapvibe_model", "seg", T("Модель", "Model"), CHEAPVIBE_MODEL_TIP,
                      options=catalog.CHEAPVIBE_MODELS),
                    F("infographic_cheapvibe_size", "combo", T("Размер", "Size"),
                      T("Размеры под выбранный формат кадра.", "Sizes for the selected frame format."),
                      options=lambda v: catalog.cheapvibe_sizes(v.get("veo_ratio")), width=180),
                    F("infographic_cheapvibe_quality", "seg", T("Качество", "Quality"), None,
                      options=[(q, q) for q in catalog.CHEAPVIBE_QUALITIES]),
                    Note(None, dynamic=lambda v: catalog.cheapvibe_cost_note(
                        v.get("infographic_cheapvibe_model"), v.get("infographic_cheapvibe_quality"))),
                ], visible=lambda v: v.get("infographic_provider") == "cheapvibe", indent=0),
                Note(VARAG_IMAGE_NOTE, visible=lambda v: v.get("infographic_provider") == "varag"),
                Group(None, [
                    F("infographic_cinai_model", "seg", T("Модель", "Model"),
                      T("Только бесплатные модели — безлимитные на тарифе CinAI.",
                        "Free models only — unlimited on the CinAI plan."),
                      options=catalog.CINAI_MODELS),
                ], visible=lambda v: v.get("infographic_provider") == "cinai", indent=0),
            ], visible=lambda v: v.get("infographic_enabled")),
        ],
    }


# ======================================================== Моушн-графика
def _motion(page):
    return {
        "key": "motion_graphics", "icon": "◭", "title": T("Моушн-графика", "Motion graphics"),
        "tip": T("Анимированная графика вместо видеокадра: счётчики, растущие графики, линии на карте,\n"
                 "таймлайны, кинетическая типографика. Где её ставить, решает режиссёр; сцену пишет\n"
                 "Codex, рисует Remotion.",
                 "Animated graphics instead of a video shot: counters, growing charts, lines on a map,\n"
                 "timelines, kinetic typography. The director decides where; Codex writes the scene,\n"
                 "Remotion renders it."),
        "items": [
            F("motion_enabled", "bool", T("Вставлять моушн-графику (Remotion)", "Insert motion graphics (Remotion)"),
              T("Режиссёр отводит под графику клипы с цифрами, датами, маршрутами, сравнениями.\n"
                "Для каждого Codex пишет анимацию, Remotion рендерит её видеоклипом точной длины.\n"
                "Не вышло — клип уходит в обычную нейрогенерацию. Доля — в разделе «Состав видео».",
                "The director reserves clips with numbers, dates, routes and comparisons for graphics.\n"
                "Codex writes an animation for each, Remotion renders it as a clip of the exact length.\n"
                "If it fails, the clip goes to regular AI generation. The share is in 'Video mix'.")),
            Group(None, [
                F("motion_model", "seg", T("Модель Codex", "Codex model"),
                  T("Какая модель Codex пишет сцены. Мощнее — аккуратнее композиция и анимация,\n"
                    "быстрее — дешевле по квоте и быстрее готова. Список берётся у самого Codex.",
                    "Which Codex model writes the scenes. Stronger — neater layout and motion,\n"
                    "faster — lighter on quota and quicker. The list comes from Codex itself."),
                  options=lambda v: catalog.codex_options()),
                F("motion_parallel", "int", T("Потоков", "Threads"),
                  T("Сколько клипов готовить одновременно. Каждый поток — отдельный запрос к Codex\n"
                    "и рендер; больше — быстрее, но сильнее расходует квоту и процессор.",
                    "How many clips at once. Each thread is a separate Codex request and render;\n"
                    "more is faster but uses more quota and CPU."), lo=1, hi=8),
                F("motion_gate_sec", "int", T("Графика с секунды", "Graphics from second"),
                  T("С какой секунды ролика допустима графика. Вступление — хук. 0 = без ограничения.",
                    "From which second graphics are allowed. The intro is the hook. 0 = no limit."),
                  lo=0, hi=300, step=5),
                Custom(page.build_remotion_status),
            ], visible=lambda v: v.get("motion_enabled")),
        ],
    }


# ======================================================= Генерация картинок
def _images(page):
    return {
        "key": "imagegen", "icon": "◩", "title": T("Генерация картинок", "Image generation"),
        "items": [
            F("img_gen", "seg", T("Провайдер", "Provider"),
              T("Чем генерировать картинки для сцен:\n"
                "• VeoNonStop — модели Banana.\n"
                "• Vertex — Google Vertex AI (модели Banana).\n"
                "• CheapVibeCode — GPT Image 2, Grok Imagine или Banana 2 по API (оплата токенами).\n"
                "• VARAG — картинки по тексту через Inflight API (квота тарифа).\n"
                "• CinAI — генерация Studio.\n"
                "• Пропустить — не генерировать (для t2v, где видео делается прямо из текста).",
                "What generates scene images:\n"
                "• VeoNonStop — Banana models.\n"
                "• Vertex — Google Vertex AI (Banana models).\n"
                "• CheapVibeCode — GPT Image 2, Grok Imagine or Banana 2 via API (paid in tokens).\n"
                "• VARAG — text-to-image via the Inflight API (plan quota).\n"
                "• CinAI — Studio generation.\n"
                "• Skip — don't generate (for t2v where video is made from text)."),
              options=catalog.IMAGE_PROVIDERS + [("skip", T("Пропустить", "Skip"))]),
            _frame_format(visible=lambda v: v.get("img_gen") != "skip"),
            F("veo_quota_t2v_fallback", "bool", T("Квота VeoNonStop исчерпана → T2V", "VeoNonStop quota out → T2V"),
              T("Включено: остаток клипов пойдёт в text-to-video, когда картинки VeoNonStop исчерпаны.\n"
                "Недоступно при «Слайдшоу» и без генерации видео.",
                "On: remaining clips go to text-to-video when VeoNonStop images are exhausted.\n"
                "Unavailable for Slideshow and without video generation."),
              visible=lambda v: v.get("img_gen") == "veononstop",
              enabled=lambda v: _video_used(v) and not _video_off(v)),
            Group(None, [
                F("veo_banana_model", "seg", T("Модель", "Model"),
                  T("Версия модели Banana. Новее — обычно качественнее.",
                    "Banana model version. Newer is usually better."),
                  options=catalog.BANANA_MODELS),
                Custom(page.build_img_keys),
                Row(F("veo_imggen_parallel", "int", T("Потоков", "Workers"),
                      T("Сколько картинок генерировать одновременно. Потолок — лимит вашего ключа.",
                        "How many images at once. Capped by your key's limit."), lo=1, hi=48),
                    F("image_upscale", "combo", T("апскейл", "upscale"),
                      T("Увеличивать разрешение готовых картинок. 2K/4K — чётче, но дольше.",
                        "Upscale finished images. 2K/4K is sharper but slower."),
                      options=[("off", T("выкл", "off")), "2K", "4K"])),
            ], visible=lambda v: v.get("img_gen") == "veononstop", indent=0),
            Group(None, [
                F("vertex_image_model", "seg", T("Модель", "Model"), VERTEX_MODEL_TIP,
                  options=catalog.VERTEX_IMAGE_MODELS),
                F("vertex_image_parallel", "int", T("Потоков", "Threads"),
                  T("Сколько картинок генерировать одновременно. Больше — быстрее, но Vertex чаще\n"
                    "отвечает отказом по квоте.",
                    "How many images at once. More is faster, but Vertex rejects more often on quota."),
                  lo=1, hi=16),
            ], visible=lambda v: v.get("img_gen") == "vertex", indent=0),
            Group(None, [
                F("cheapvibe_model", "seg", T("Модель", "Model"), CHEAPVIBE_MODEL_TIP,
                  options=catalog.CHEAPVIBE_MODELS),
                Row(F("cheapvibe_size", "combo", T("Размер", "Size"),
                      T("Размер под выбранный формат кадра: 16:9 — 1536×1024 (у модели нет точного 16:9,\n"
                        "склейка подгонит кадр), 9:16 — 1024×1536.",
                        "Size for the selected frame format: 16:9 — 1536×1024 (the model has no exact 16:9,\n"
                        "the render fits the frame), 9:16 — 1024×1536."),
                      options=lambda v: catalog.cheapvibe_sizes(v.get("veo_ratio")), width=180),
                    F("cheapvibe_parallel", "int", T("потоков", "threads"),
                      T("Сколько картинок генерировать одновременно.", "How many images at once."), lo=1, hi=16)),
                F("cheapvibe_quality", "seg", T("Качество", "Quality"),
                  T("Качество меняет детализацию, а не разрешение. Списание фиксированное за картинку.",
                    "Quality changes detail, not resolution. The charge is fixed per image."),
                  options=[(q, q) for q in catalog.CHEAPVIBE_QUALITIES]),
                Note(None, dynamic=lambda v: catalog.cheapvibe_cost_note(v.get("cheapvibe_model"),
                                                                        v.get("cheapvibe_quality"))),
            ], visible=lambda v: v.get("img_gen") == "cheapvibe", indent=0),
            Group(None, [
                Note(VARAG_IMAGE_NOTE),
                F("varag_image_parallel", "int", T("Потоков", "Threads"), VARAG_PARALLEL_TIP, lo=1, hi=16),
            ], visible=lambda v: v.get("img_gen") == "varag", indent=0),
            Group(None, [
                F("cinai_model", "seg", T("Модель", "Model"),
                  T("Только бесплатные модели — безлимитные на тарифе CinAI.",
                    "Free models only — unlimited on the CinAI plan."),
                  options=catalog.CINAI_MODELS),
                F("cinai_parallel", "int", T("Потоков", "Threads"),
                  T("Потоков CinAI. Больше, чем разрешает тариф, одновременно всё равно не пойдёт.",
                    "CinAI workers. The plan's concurrency limit still applies."), lo=1, hi=6),
            ], visible=lambda v: v.get("img_gen") == "cinai", indent=0),
        ],
    }


# ============================================================ Ремиксер
def _remixer(page):
    return {
        "key": "remixer", "icon": "⟲", "title": T("Ремиксер", "Remixer"),
        "tip": T("Чем перерисовывать референс или кадр, если генератор его отклонил (цензура): картинку\n"
                 "перерисовывают в другом ракурсе/стиле, сохраняя смысл сцены.",
                 "What redraws a reference or frame when the generator rejects it (moderation): the\n"
                 "picture is redrawn from another angle/style keeping the scene meaning."),
        "items": [
            F("remix_engine", "seg", T("Движок", "Engine"),
              T("• CinAI — бесплатные модели CinAI (картинка → картинка). Нужна сессия CinAI в «Настройках».\n"
                "• Выкл — не перерисовывать.",
                "• CinAI — free CinAI models (image → image). Needs a CinAI session in Settings.\n"
                "• Off — don't redraw."),
              options=catalog.REMIX_ENGINES),
            F("cinai_remix_model", "seg", T("Модель CinAI", "CinAI model"),
              T("Бесплатные модели CinAI, которые перерисовывают кадр по исходнику.",
                "Free CinAI models that redraw a frame from the source."),
              options=catalog.CINAI_REMIX_MODELS, visible=lambda v: v.get("remix_engine") == "cinai"),
            _frame_format(visible=lambda v: v.get("remix_engine") != "off"),
        ],
    }


# ======================================================== Видеогенерация
def _video_off(v):
    return video_off(v)


def _video_used(v):
    """Видео сцен генерируется: не «Слайдшоу» (там только картинки)."""
    return not slideshow(v)


def _veo_video(v):
    """Видео делает сам VeoNonStop (ключи-слоты, капча, апскейл — только у него)."""
    return not _video_off(v) and v.get("video_provider") == "veononstop"


def _veo_i2v(v):
    return v.get("video_mode") == "i2v"


def _parallax_supported(v):
    return parallax_supported(v)


def _video(page):
    return {
        "key": "videogen", "icon": "▷", "title": T("Генерация видео", "Video generation"),
        "items": [
            Note(T("Слайдшоу: видео сцен не генерируется, поэтому провайдер, режим и промпты видео не действуют.\n"
                   "Параллакс работает: оживляет отмеченные кадры через VeoNonStop.",
                   "Slideshow: scene videos are not generated, so the video provider, mode and prompts do not apply.\n"
                   "Parallax still works: it animates the marked frames through VeoNonStop."),
                 role="warn", visible=slideshow),
            F("video_provider", "seg", T("Провайдер", "Provider"),
              T("Чем генерировать видео:\n• VeoNonStop — сервис VeoNonStop.\n"
                "• Vertex — Google Vertex AI (Veo 3.1 / 3 / 2). Ключ и PROJECT_ID — в «Настройках».\n"
                "• RoyalTechno — Veo 3.1 (720p, клип 8 с), $0.02 за клип. Ключ — в «Настройках».\n"
                "• VARAG — видео через Inflight API (квота тарифа). Ключ — в «Настройках».\n"
                "• CinAI — бесплатные видеомодели CinAI (безлимит по тарифу). Сессия — в «Настройках».\n"
                "• Пропустить — видео не генерировать: картинки остаются статикой, склейка добавит им\n"
                "  движение (Ken Burns).",
                "What generates video:\n• VeoNonStop — the VeoNonStop service.\n"
                "• Vertex — Google Vertex AI (Veo 3.1 / 3 / 2). Key and PROJECT_ID are in Settings.\n"
                "• RoyalTechno — Veo 3.1 (720p, 8 s clip), $0.02 per clip. Key is in Settings.\n"
                "• VARAG — video via the Inflight API (plan quota). Key is in Settings.\n"
                "• CinAI — free CinAI video models (unlimited on the plan). Session is in Settings.\n"
                "• Skip — don't generate video: images stay still, the render adds motion (Ken Burns)."),
              options=catalog.VIDEO_PROVIDERS + [("skip", T("Пропустить", "Skip"))], enabled=_video_used),
            F("video_mode", "seg", T("Режим", "Mode"),
              T("Как делать видео из сцены:\n"
                "• i2v — оживить готовую картинку.\n• t2v — видео прямо из текста, без картинки\n"
                "  (картинки при этом не нужны и выключаются).",
                "How to make a video from a scene:\n"
                "• i2v — animate a finished image.\n• t2v — video straight from text, no image\n"
                "  (images are not needed and get turned off)."),
              options=[("i2v", "i2v"), ("t2v", "t2v")], visible=lambda v: not _video_off(v), enabled=_video_used),
            _frame_format(),
            Custom(page.build_video_keys, visible=_veo_video),
            Group(None, [
                Note(T("Модель видео: Veo 3.1 Lite.", "Video model: Veo 3.1 Lite.")),
                Row(F("vertex_video_resolution", "combo", T("разрешение", "resolution"),
                      T("Разрешение клипа.", "Clip resolution."),
                      options=catalog.VERTEX_RESOLUTIONS),
                    F("vertex_video_parallel", "int", T("потоков", "threads"),
                      T("Сколько клипов генерировать одновременно. У Vertex жёсткие лимиты запросов\n"
                        "в минуту: при отказах по квоте уменьшите.",
                        "How many clips at once. Vertex has strict per-minute limits: lower this on quota errors."),
                      lo=1, hi=16)),
                F("custom_duration", "bool", T("Длительность под сцену (4–8 с)", "Duration per scene (4–8 s)"),
                  T("Просить клип той длины, что нужна сцене: 4, 6 или 8 секунд (ближайшее сверху).\n"
                    "Без галки клип всегда 8 секунд.",
                    "Request a clip as long as the scene needs: 4, 6 or 8 seconds (next up).\n"
                    "Without it every clip is 8 seconds.")),
            ], visible=lambda v: not _video_off(v) and v.get("video_provider") == "vertex", indent=0,
                enabled=_video_used),
            Group(None, [
                Note(T("Модель видео: Veo 3.1 · 720p · клип 8 с · $0.02 за клип.",
                       "Video model: Veo 3.1 · 720p · 8 s clip · $0.02 per clip.")),
                F("royaltechno_video_parallel", "int", T("Потоков", "Threads"),
                  T("Сколько клипов генерировать одновременно. Сервис сам ограничивает число задач\n"
                    "в работе по вашему тарифу (2–10, не больше 20): больше — не быстрее, лишнее\n"
                    "просто подождёт в очереди.",
                    "How many clips at once. The service caps jobs in flight by your plan (2–10,\n"
                    "never above 20): more is not faster, the extra just waits in the queue."),
                  lo=1, hi=20),
            ], visible=lambda v: not _video_off(v) and v.get("video_provider") == "royaltechno", indent=0,
                enabled=_video_used),
            Group(None, [
                Note(T("Модель и длину клипа выбирает сам сервис: «Длительность под сцену» здесь не действует,\n"
                       "лишнее склейка подрежет.",
                       "The service picks the model and clip length: 'Duration per scene' does not apply here,\n"
                       "the render trims the extra.")),
                F("varag_video_parallel", "int", T("Потоков", "Threads"), VARAG_PARALLEL_TIP, lo=1, hi=16),
            ], visible=lambda v: not _video_off(v) and v.get("video_provider") == "varag", indent=0,
                enabled=_video_used),
            Group(None, [
                F("cinai_video_model", "seg", T("Модель", "Model"),
                  T("Клип выходит 720p.\n"
                    "• Бесплатно (безлимит на тарифе CinAI): CinGen Video 1.0, Veo 3.1 Fast, Veo 3.1 Lite,\n"
                    "  Cin Flash 1.1, Seedance 2.5 Dola, Seedance 2.0 Dola.\n"
                    "• По квоте: Gemini Omni 1.1 Flash и Grok video — на тарифе Starter 20 клипов в месяц,\n"
                    "  дальше 25 и 60 кредитов за клип (на Pro Omni безлимитна, у Grok 60 в месяц).\n"
                    "Длина клипа: CinGen Video и Cin Flash — 3…10 с, Veo — 4/6/8 с, Seedance — 5/10 с,\n"
                    "Omni — 4/6/8/10 с, Grok — 6/10/15 с (ближайшая сверху; лишнее склейка подрежет).\n"
                    "При оживлении картинки CinGen Video 1.0 и Veo длину не принимают и отдают 8–10 с.",
                    "Clips are 720p.\n"
                    "• Free (unlimited on the CinAI plan): CinGen Video 1.0, Veo 3.1 Fast, Veo 3.1 Lite,\n"
                    "  Cin Flash 1.1, Seedance 2.5 Dola, Seedance 2.0 Dola.\n"
                    "• Quota: Gemini Omni 1.1 Flash and Grok video — 20 clips a month on Starter,\n"
                    "  then 25 and 60 credits per clip (on Pro Omni is unlimited, Grok has 60 a month).\n"
                    "Clip length: CinGen Video and Cin Flash — 3…10 s, Veo — 4/6/8 s, Seedance — 5/10 s,\n"
                    "Omni — 4/6/8/10 s, Grok — 6/10/15 s (next length up; the render trims the extra).\n"
                    "When animating an image CinGen Video 1.0 and Veo ignore the length and give 8–10 s."),
                  options=catalog.CINAI_VIDEO_MODELS),
                F("custom_duration", "bool", T("Длительность под сцену", "Duration per scene"),
                  T("Просить клип той длины, что нужна сцене (ближайшее сверху из того, что умеет модель —\n"
                    "ряды длин в подсказке к модели). Без галки все клипы одной длины — по самой длинной сцене.\n"
                    "При оживлении картинки (i2v) CinGen Video 1.0, Veo 3.1 Fast и Veo 3.1 Lite длину\n"
                    "не принимают и всегда отдают 8–10 с — с ними галка работает только в t2v.",
                    "Request a clip as long as the scene needs (next length up the model supports — see\n"
                    "the model tooltip). Without it every clip has one length — that of the longest scene.\n"
                    "When animating an image (i2v) CinGen Video 1.0, Veo 3.1 Fast and Veo 3.1 Lite ignore\n"
                    "the length and always give 8–10 s — with them it works only in t2v."),
                  enabled=lambda v: v.get("video_mode") == "t2v"
                  or v.get("cinai_video_model") not in catalog.CINAI_VIDEO_FIXED_I2V_LENGTH),
                F("cinai_video_parallel", "int", T("Потоков", "Threads"),
                  T("Сколько клипов генерировать одновременно. Больше, чем разрешает тариф CinAI,\n"
                    "одновременно всё равно не пойдёт.",
                    "How many clips at once. The CinAI plan's concurrency limit still applies."), lo=1, hi=6),
            ], visible=lambda v: not _video_off(v) and v.get("video_provider") == "cinai", indent=0,
                enabled=_video_used),
            F("inflight_mode", "bool", T("Оживлять сразу (inflight)", "Animate right away (inflight)"),
              T("Запускать оживление сцены сразу, как только её картинка готова, не дожидаясь остальных.\n"
                "Генерация картинок и видео идут внахлёст.",
                "Start animating a scene as soon as its image is ready, without waiting for the rest.\n"
                "Image and video generation overlap."),
              enabled=lambda v: v.get("video_mode") == "i2v" and _video_used(v)),
            Row(F("i2v_parallax", "bool", T("Параллакс", "Parallax"),
                  T("В обычном i2v заменяет промпт сцены шаблоном параллакса. В авто-видеоистории\n"
                    "оживляет каждый N-й готовый кадр источников; остальные остаются с зумом.",
                    "In regular i2v replaces the scene prompt with a parallax template. In the auto\n"
                    "visual story it animates every N-th source frame; the rest keep the zoom."),
                  enabled=_parallax_supported),
                F("i2v_parallax_method", "combo", None,
                  T("0 Вперемешку: клипы получают разные шаблоны.\n1 Balanced: мягкий естественный параллакс.\n"
                    "2 Strict: двигается только камера.\n3 Mechanical: заметное механическое движение.",
                    "0 Mixed: clips get different templates.\n1 Balanced: soft natural parallax.\n"
                    "2 Strict: only the camera moves.\n3 Mechanical: noticeable mechanical motion."),
                  options=[("0", T("0 вперемешку", "0 mixed")), ("1", "1 Balanced"), ("2", "2 Strict"),
                           ("3", "3 Mechanical")],
                  enabled=lambda v: _parallax_supported(v) and v.get("i2v_parallax"))),
            Row(F("i2v_parallax_ai", "bool", T("Нейрокадрам", "AI frames"),
                  T("Нейрокадры: параллакс-промпт вместо обычного в i2v.", "AI frames: parallax prompt instead of the regular one."),
                  enabled=_parallax_supported),
                F("i2v_parallax_mix_perclip", "combo", None,
                  T("Что делать с нейрокадрами:\n• вперемешку — параллакс получает каждый N-й, остальные\n"
                    "  оживляются промптами своих сцен;\n• всем — параллакс получают все нейрокадры.",
                    "What to do with AI frames:\n• mixed — every N-th gets parallax, the rest are animated\n"
                    "  with their scene prompts;\n• all — every AI frame gets parallax."),
                  options=[(True, T("вперемешку с промптами", "mixed with prompts")), (False, T("всем", "all"))],
                  enabled=lambda v: _veo_i2v(v) and v.get("i2v_parallax")),
                F("i2v_parallax_stock", "bool", T("Сток-фото", "Stock photos"),
                  T("Сток-фото — отдельным проходом VeoNonStop i2v.", "Stock photos — a separate VeoNonStop i2v pass."),
                  enabled=_parallax_supported),
                label=T("Кому параллакс", "Parallax for")),
            Row(F("i2v_parallax_every_n", "int", T("Каждый N-й", "Every N-th"),
                  T("Отмеченные клипы складываются в один ряд, и параллакс получает каждый N-й.\n"
                    "1 = все, 2 = каждый второй.",
                    "Marked clips form one row and every N-th of them gets parallax.\n1 = all, 2 = every second."),
                  lo=1, hi=999,
                  enabled=lambda v: _parallax_supported(v) and v.get("i2v_parallax") and (
                      _video_off(v) or v.get("i2v_parallax_mix_perclip"))),
                F("video_live_animate", "bool", T("Оживлять по мере появления кадров", "Animate as frames appear"),
                  T("Оживлять кадр сразу после появления, не дожидаясь конца генерации картинок.\n"
                    "Если картинки и видео идут на одном ключе VeoNonStop — оживление подождёт.",
                    "Animate a frame as soon as it appears, without waiting for all images.\n"
                    "If images and video use the same VeoNonStop key, animation waits."))),
            Row(F("veo_parallel", "int", T("Потоков видео", "Video workers"),
                  T("Сколько видео-сцен генерировать одновременно. Потолок — лимит вашего ключа.",
                    "How many video scenes at once. Capped by your key's limit."), lo=1, hi=48),
                F("veo_request_delay", "float", T("пауза", "delay"),
                  T("Пауза между отправкой задач (сек). Больше — мягче к сервису.",
                    "Delay between task submissions (sec). Larger is gentler on the service."),
                  lo=0.5, hi=30, step=0.5, suffix=SEC),
                F("video_upscale", "bool", T("Апскейл до 1080p", "Upscale to 1080p"),
                  T("Увеличить готовое видео до 1080p. Блокирует выбор 1080p/4K в склейке.",
                    "Upscale finished videos to 1080p. Locks 1080p/4K in the render.")),
                visible=_veo_video),
            F("veo_advanced_visible", "bool", T("Капча / повторы", "Captcha / retries"),
              T("Раскрыть тонкие параметры VEO: паузу перед reCAPTCHA, число повторов и «тихих» циклов.\n"
                "Обычно менять не нужно.",
                "Show fine VEO settings: reCAPTCHA delay, retries and silent cycles.\nUsually no need to change."),
              visible=_veo_video),
            Row(F("veo_recaptcha_delay", "float", T("reCAPTCHA", "reCAPTCHA"),
                  T("Пауза перед прохождением reCAPTCHA (сек).", "Delay before reCAPTCHA (sec)."),
                  lo=1, hi=60, step=1, suffix=SEC),
                F("veo_recaptcha_jitter", "float", T("разброс", "jitter"),
                  T("Случайный разброс к паузе (±сек).", "Random jitter added to the delay (±sec)."),
                  lo=0.5, hi=30, step=0.5, suffix=SEC),
                F("veo_max_retries", "int", T("повторов", "retries"),
                  T("Сколько раз повторить запрос при ошибке.", "How many times to retry a failed request."),
                  lo=1, hi=5),
                F("veo_max_silent_cycles", "int", T("циклов", "cycles"),
                  T("Сколько «тихих» кругов без прогресса пройти перед переходом на запасной путь.",
                    "How many silent cycles without progress before the fallback."), lo=1, hi=10),
                visible=lambda v: v.get("veo_advanced_visible") and _veo_video(v)),
            F("veo_fast_censor", "combo", T("Обход отказов", "Rejection handling"),
              T("Как быстро сдаваться по клипу, который генератор отклоняет:\n"
                "• off — полная цепочка переписываний и перерисовок.\n"
                "• after_rewrites — после переписываний промпта сразу на запасной путь.\n"
                "• ultrafast — сразу на запасной путь.",
                "How fast to give up on a clip the generator rejects:\n"
                "• off — the full rewrite/redraw chain.\n"
                "• after_rewrites — fallback right after prompt rewrites.\n"
                "• ultrafast — fallback immediately."),
              options=["off", "after_rewrites", "ultrafast"], visible=lambda v: not _video_off(v),
              enabled=_video_used),
            F("prompt_mode", "seg", T("Промпты видео", "Video prompts"),
              T("Откуда брать промпты для видео:\n• Общий — один стиль-промпт на все сцены (файл ниже).\n"
                "• Свой для сцены — промпт режиссёра для каждой сцены (живее и точнее).",
                "Where video prompts come from:\n• Single — one style prompt for all scenes (file below).\n"
                "• Per clip — the director's prompt for each scene (livelier, more precise)."),
              options=[("single", T("Общий", "Single")), ("perclip", T("Свой для сцены", "Per clip"))],
              enabled=_video_used),
            F("single_prompt_path", "file", T("Файл промпта", "Prompt file"),
              T("Текстовый файл с общим промптом для видео (single).", "Text file with the single video prompt."),
              filters="Text (*.txt *.md);;All (*.*)", visible=lambda v: v.get("prompt_mode") == "single",
              enabled=_video_used),
            F("codex_adapt_var", "bool", T("Адаптировать промпты под видео (GPT)", "Adapt prompts for video (GPT)"),
              T("Переписать промпты сцен через GPT под оживление картинки: добавить движение камеры и\n"
                "объектов по вашему стайлгайду.",
                "Rewrite scene prompts with GPT for image animation: add camera and subject motion\n"
                "following your style guide."),
              visible=lambda v: v.get("prompt_mode") == "perclip", enabled=_video_used),
            F("rewrite_model", "seg", T("Переписывание", "Rewrite model"),
              T("Модель, которая переписывает промпт, если генерацию заблокировал модератор: смягчает\n"
                "формулировки (убирает имена, крупные планы травм и т.п.), сохраняя смысл сцены.",
                "Model that rewrites a prompt blocked by moderation: softens wording (removes names,\n"
                "close-ups of injuries, etc.) keeping the scene meaning."),
              options=lambda v: [(spec, label) for label, spec in catalog.REWRITE_MODELS]),
        ],
    }


# ========================================================= Состав видео
def _mix(page):
    return {
        "key": "source_mix", "icon": "▤", "title": T("Состав видео", "Video mix"),
        "tip": T("Какая доля клипов ролика берётся из стока, отдаётся инфографике и моушн-графике\n"
                 "и генерируется нейросетью. Пока включена только нейрогенерация, делить нечего —\n"
                 "ползунок недоступен.",
                 "What share of the clips comes from stock, goes to infographics and motion graphics\n"
                 "and is generated by AI. While only AI generation is on there is nothing to split —\n"
                 "the slider is disabled."),
        "items": [Custom(page.build_source_mix)],
    }


# ======================================================= Рендер и склейка
def _render(page):
    return {
        "key": "render_concat", "icon": "⬒", "title": T("Рендер и склейка", "Render & concat"),
        "items": [
            F("render_mode", "seg", T("Качество", "Quality"),
              T("Как собирать финальное видео:\n• Быстро — склейка без перекодирования.\n"
                "• 1080p / 4K — пересобрать в этом разрешении (чётче, но дольше).",
                "How the final video is built:\n• Fast — join without re-encoding.\n"
                "• 1080p / 4K — re-encode at this resolution (sharper but slower)."),
              options=[("720p", T("Быстро", "Fast")), ("1080p", "1080p"), ("4k", "4K")]),
            Row(F("concat_bitrate", "combo", T("Битрейт", "Bitrate"),
                  T("Битрейт готового видео. Выше — чётче и тяжелее файл.",
                    "Bitrate of the final video. Higher is sharper and heavier."),
                  options=["5M", "8M", "10M", "15M", "20M", "25M", "30M", "35M", "40M"]),
                F("concat_fps", "combo", "FPS", T("Частота кадров. 60 — плавно, 24–30 — «киношнее».",
                                                   "Frame rate. 60 is smooth, 24–30 is more cinematic."),
                  options=[24, 25, 30, 50, 60])),
            F("concat_queue_enabled", "bool", T("Очередь склейки в фоне", "Background concat queue"),
              T("Склейка уходит в фоновую очередь (видна в «Панели управления»), а генерация сразу берёт\n"
                "следующий файл. Склейки идут строго по одной.",
                "Concatenation goes to a background queue (see Control Panel) while generation takes\n"
                "the next file. Renders run strictly one at a time.")),
            F("infographic_static_concat", "bool", T("Инфографика без зума", "Keep infographics static"),
              T("Только для инфографики: неподвижный кадр без зума.", "Infographics only: a still frame without zoom.")),
            Row(F("transitions_enabled", "bool", T("Переходы", "Transitions"),
                  T("Плавные переходы между клипами (fade, wipe, dissolve…). Работают на 1080p/4K.\n"
                    "Длительность клипов сохраняется.",
                    "Smooth transitions between clips (fade, wipe, dissolve…). Work at 1080p/4K.\n"
                    "Clip durations are preserved.")),
                F("transition_mode", "seg", None, None,
                  options=[("fixed", T("один", "fixed")), ("random", T("случайно", "random"))],
                  enabled=lambda v: v.get("transitions_enabled")),
                F("transition_style_fixed", "combo", None,
                  T("Какой переход (для режима «один»).", "Which transition (fixed mode)."),
                  options=catalog.TRANSITION_STYLES,
                  enabled=lambda v: v.get("transitions_enabled") and v.get("transition_mode") == "fixed"),
                F("transition_duration", "float", T("длит.", "dur."),
                  T("Длительность перехода (сек).", "Transition duration (sec)."), lo=0.2, hi=2.0, step=0.1,
                  enabled=lambda v: v.get("transitions_enabled"))),
            Row(F("zoom_mode", "seg", None, None, options=[("fixed", T("один", "fixed")), ("random", T("случайно", "random"))]),
                F("zoom_style_fixed", "combo", None,
                  T("Куда двигать зум (наезд/отъезд × точка кадра). in-center — классика.",
                    "Zoom direction (in/out × frame point). in-center is the classic."),
                  options=catalog.ZOOM_STYLES, enabled=lambda v: v.get("zoom_mode") == "fixed"),
                label=T("Зум слайдов", "Slide zoom"),
                tip=T("Эффект Ken Burns для картинок (слайдшоу/вислайд).", "Ken Burns effect for stills (slideshow/vislide).")),
            F("zoom_supersample", "combo", T("Плавность зума", "Zoom smoothness"),
              T("«Как было» — зум шагает целыми пикселями и подрагивает на медленном наезде.\n"
                "«Суперсемпл ×2» — движение считается крупнее и ужимается в конце: дрожь уходит,\n"
                "склейка ~+20% времени. «×3» — ещё глаже, ~+65%.",
                "'Legacy' — zoom steps in whole pixels and jitters on slow moves.\n"
                "'Supersample ×2' — motion is computed larger and scaled down: no jitter,\n"
                "~+20% render time. '×3' — even smoother, ~+65%."),
              options=[("1", T("как было", "legacy")), ("2", T("суперсемпл ×2", "supersample ×2")),
                       ("3", T("суперсемпл ×3", "supersample ×3"))]),
            F("watermark", "seg", T("Логотип канала", "Channel logo"),
              T("Наложить свой логотип (брендинг канала) в углу кадра.", "Overlay your channel logo in a corner."),
              options=[("as_is", T("нет", "none")), ("logo", T("логотип", "logo"))]),
            Row(F("logo_mode", "seg", None,
                  T("• Один на все — один логотип для всех роликов.\n• Для каждого — в таблице очереди.",
                    "• One for all — one logo for every video.\n• Per file — in the queue table."),
                  options=[("one_for_all", T("один на все", "one for all")), ("individual", T("для каждого", "per file"))]),
                F("logo_path", "file", None, T("Файл логотипа (PNG с прозрачностью).", "Logo file (PNG with transparency)."),
                  filters="Images (*.png *.jpg *.jpeg *.webp)",
                  visible=lambda v: v.get("logo_mode") == "one_for_all"),
                visible=lambda v: v.get("watermark") == "logo"),
            Row(F("keep_files", "bool", T("Хранить промежуточные файлы", "Keep retiming files"),
                  T("Не удалять промежуточные видео подгонки сцен. Полезно для разбора проблем.",
                    "Keep intermediate retimed videos. Useful for debugging.")),
                F("keep_veo_sound", "bool", T("Сохранить звук VEO", "Keep VEO sound"),
                  T("Сохранить родной звук сгенерированных видео отдельной дорожкой (retimed_sound/).\n"
                    "Само видео остаётся с озвучкой.",
                    "Save the generated videos' own sound as a separate track (retimed_sound/).\n"
                    "The video keeps the voice-over."))),
            Row(F("subtitles_enabled", "bool", T("Вжечь субтитры", "Burn subtitles"),
                  T("Вжечь субтитры в видео (по расшифровке озвучки).", "Burn subtitles into the video (from the transcript).")),
                F("subtitles_preset", "seg", None,
                  T("Снизу — классика, по центру — крупный «вирусный» стиль.", "Bottom — classic, center — large viral style."),
                  options=[("bottom", T("снизу", "bottom")), ("center", T("по центру", "center"))],
                  enabled=lambda v: v.get("subtitles_enabled") and not v.get("subtitles_custom")),
                F("subtitles_custom", "bool", T("свой шаблон", "custom template"),
                  T("Свой файл .ass вместо встроенного шаблона. Включает и сами субтитры.",
                    "Your own .ass file instead of the built-in template. Also turns subtitles on."))),
            F("subtitles_path", "file", T("Шаблон .ass", ".ass template"), None, filters="ASS (*.ass)",
              visible=lambda v: v.get("subtitles_enabled") and v.get("subtitles_custom")),
            Row(F("subtitle_phrase_len", "int", T("Длина фразы", "Phrase length"),
                  T("На куски какой длины резать фразу субтитра. «Слов» + 1 — каждое слово отдельно.",
                    "How long each subtitle phrase is. 'Words' + 1 — every word separately."), lo=1, hi=120),
                F("subtitle_phrase_unit", "seg", None, None,
                  options=[("chars", T("символов", "chars")), ("words", T("слов", "words"))]),
                visible=lambda v: v.get("subtitles_enabled")),
        ],
    }
