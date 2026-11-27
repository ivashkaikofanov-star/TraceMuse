"""Commercial-use decisions for an image, based on the published licence evidence."""

SHARE_ALIKE = 'Производные работы распространяются на совместимых условиях ShareAlike'


def photo_credit(material: dict, modified: bool = False) -> str:
    licence = material['license']
    author = f" — {material['author']}" if material['author'] else ''
    change = ' · Изображение изменено.' if modified else ''
    return f"«{material['title']}»{author}. Источник: {material['url']}. {licence['name']}: {licence['url']}.{change}"


def _decision(category, label, action, detail, *, warning='', changes=''):
    return {'category': category, 'label': label, 'action': action, 'detail': detail,
            'warning': warning, 'changes': changes}


def commercial_conditions(matches: list[dict], *, branded: bool, modified: bool) -> dict:
    exact = [item for item in matches if item['match_kind'] in {'bytes', 'pixels'}]
    label = 'Условия не найдены или есть только перепечатка'
    if not exact:
        if branded:
            return _decision('unknown', label, 'unverified',
                             'Распознан логотип. Разрешение на использование изображения и товарного знака не установлено. Подпись его не заменяет.')
        return _decision('unknown', label, 'unverified',
                         'Разрешение на коммерческое использование не установлено. Подпись сама по себе его не даёт.')
    source = exact[0]
    licence = source['license']
    kind = licence.get('kind')
    if not kind:
        return _decision('unknown', label, 'unverified',
                         'Изображение совпало, но источник не подтвердил условия коммерческого использования этого файла.',
                         warning='Для распознанного знака нужно отдельное основание использования.' if branded else '')
    if len({(item['author'], item['license'].get('kind'), item['license'].get('url'),
             tuple(item['license'].get('restrictions', []))) for item in exact}) > 1:
        return _decision('unknown', 'Противоречивые сведения об изображении', 'unverified',
                         'В найденных точных копиях указаны разные авторы или условия. Разрешение не установлено.')
    if licence.get('commercial_allowed') is False:
        return _decision(kind, licence['name'], 'restricted',
                         'Лицензия не разрешает коммерческое использование. Подпись не поможет: нужно отдельное разрешение или замена.')
    if branded:
        return _decision(kind, licence['name'], 'unverified',
                         'Условия на изображение найдены. Использование распознанного товарного знака требует отдельного основания; подписи для этого недостаточно.')
    if any(item != SHARE_ALIKE for item in licence.get('restrictions', [])):
        return _decision(kind, licence['name'], 'conditional',
                         'Источник указывает дополнительные ограничения. Готовая подпись сама по себе их не покрывает.',
                         warning=' '.join(item for item in licence['restrictions'] if item != SHARE_ALIKE))
    changes = ''
    if licence.get('share_alike'):
        changes = 'При публикации изменённой версии нужно соблюсти ShareAlike: использовать совместимую лицензию.'
    elif licence.get('derivatives_distribution_allowed') is False:
        changes = 'Для публикации изменённой версии нужно отдельное разрешение.'
    if changes and modified:
        return _decision(kind, licence['name'], 'conditional',
                         'Для изменённого изображения одной подписи недостаточно.', changes=changes)
    if licence.get('attribution_required') is True:
        if not source['author']:
            return _decision(kind, licence['name'], 'unverified',
                             'Лицензия требует атрибуции, но подтверждённые данные для готовой подписи не найдены.', changes=changes)
        if kind == 'cc_by' and not modified:
            changes = 'Если отредактируете изображение, укажите изменения в подписи.'
        return _decision(kind, licence['name'], 'credit',
                         'Коммерческое использование разрешено при соблюдении лицензии. Добавьте готовую подпись.',
                         warning='Пропуск обязательной атрибуции нарушает условия лицензии; правообладатель может предъявить требования.',
                         changes=changes)
    if licence.get('attribution_required') is False:
        return _decision(kind, licence['name'], 'keep',
                         'По указанным условиям изображение можно оставить для коммерческого использования без обязательной подписи.',
                         warning=licence.get('usage_note', 'Условия источника относятся к изображению. Отдельные права на знаки, людей и другие объекты этим выводом не подтверждены.'))
    return _decision('unknown', label, 'unverified', 'Условия использования не подтверждены.')
