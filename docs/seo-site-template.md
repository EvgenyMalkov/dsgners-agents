# ТЗ для разработки dsgners.ru: SEO в шаблоне статьи

Цель: каждая новая статья, новость и список в «Избранном» выходит сразу «красивой» для поиска — без ручной работы.
Всё, что можно собрать из полей статьи, делает шаблон сайта. SEO-агент закрывает только то, что требует головы:
текст title/description под запрос, лид, alt, внутренние ссылки.

## 1. Поля в редакторе статьи
| Поле | Обязательно | Если пусто |
|---|---|---|
| SEO title (до 65 знаков) | нет | название статьи + « — dsgners» |
| Meta description (до 160 знаков) | нет | первые ~150 знаков лида, обрезка по слову |
| Обложка | да для публикации | — (не давать публиковать без обложки) |
| Alt у каждой картинки | нет | подпись картинки → иначе название статьи |
| Адрес (slug) | нет | транслит названия, без дат, до 60 знаков |
| noindex (галочка) | нет | выключено |

Поля SEO title и description — отдельный блок «Для поисковиков» в редакторе, свёрнутый по умолчанию.

## 2. Что шаблон выводит в `<head>` сам
- `<title>` и `<meta name="description">` из полей выше.
- `<link rel="canonical">` — абсолютный адрес статьи без параметров (`?utm`, `?page` и т.п.).
- `<html lang="ru">`.
- OpenGraph и Twitter: `og:type=article`, `og:title`, `og:description`, `og:url`, `og:image` (обложка, 1200×630,
  абсолютный URL), `og:site_name=dsgners`, `article:published_time`, `article:modified_time`, `twitter:card=summary_large_image`.
- `<meta name="robots" content="max-image-preview:large">` — крупные превью в Google Discover.
- JSON-LD (один `<script type="application/ld+json">`):

```json
{
  "@context": "https://schema.org",
  "@graph": [
    {
      "@type": "Article",
      "headline": "{название статьи, до 110 знаков}",
      "description": "{meta description}",
      "image": ["{обложка, абсолютный URL}"],
      "datePublished": "{ISO 8601 с часовым поясом, напр. 2026-11-02T10:00:00+03:00}",
      "dateModified": "{ISO 8601}",
      "author": {"@type": "Person", "name": "{автор}", "url": "https://dsgners.ru/{профиль автора}"},
      "publisher": {"@type": "Organization", "name": "dsgners", "logo": {"@type": "ImageObject", "url": "https://dsgners.ru/{логотип}.png"}},
      "mainEntityOfPage": "{адрес статьи}"
    },
    {
      "@type": "BreadcrumbList",
      "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "dsgners", "item": "https://dsgners.ru/"},
        {"@type": "ListItem", "position": 2, "name": "{раздел/рубрика}", "item": "{адрес раздела}"},
        {"@type": "ListItem", "position": 3, "name": "{название статьи}"}
      ]
    }
  ]
}
```
Для новостей вместо `Article` — `NewsArticle`.

## 3. На самой странице
- Один `<h1>` — название статьи. Подзаголовки в тексте — `<h2>`/`<h3>` (редактор не должен делать заголовки жирным текстом).
- Текст статьи внутри `<article>`.
- Видимые хлебные крошки (dsgners → рубрика → статья).
- Автор со ссылкой на профиль, дата публикации и обновления — видимо на странице.
- Картинки: `alt` (см. выше), `width`/`height`, `loading="lazy"` кроме обложки.
- Блок «Похожие статьи» (3–6 по тегам) и «Входит в подборку: …» для статей из списков в «Избранном».

## 4. Списки в «Избранном»
- Поля: название, ЧПУ-адрес, SEO title, description, описание списка (текст 500–1000 знаков), комментарий к каждому
  материалу, ручной порядок, обложка, галочка «индексировать».
- Индексируются только редакционные списки с галочкой и не меньше 8 материалов; личные — `noindex, follow`.
- JSON-LD: `CollectionPage` + `ItemList` (`ListItem` с `position`, `url`, `name`).
- Индексируемые списки — в sitemap.

## 5. Sitemap и переобход
- `sitemap.xml` (или индекс sitemap-ов) с `<lastmod>` у каждой статьи; новая статья попадает в sitemap сразу после публикации.
- Адрес sitemap указан в `robots.txt` (`Sitemap: https://dsgners.ru/sitemap.xml`).
- Файл ключа IndexNow в корне: `https://dsgners.ru/<ключ>.txt`, внутри — сам ключ (ключ даст владелец, он же в `.env` агентов как `INDEXNOW_KEY`).
  Отправку в IndexNow при публикации делает мониторинг агентов — на сайте ничего дополнительно не нужно.

## 6. Как проверить
- `/seo_page <адрес новой статьи>` в Telegram-боте агентов: не должно быть пунктов «Нет meta description»,
  «Нет og:image», «Нет разметки Article», «Нет canonical».
- Яндекс Вебмастер → «Валидатор микроразметки»; Google — Rich Results Test.
