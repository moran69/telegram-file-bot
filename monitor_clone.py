"""TDL media download plus receipted MTProto publication (not TDL forward stdout)."""
from pathlib import Path

from telethon import functions, types, utils


def media_key(post):
    media = post.media
    if isinstance(media, types.MessageMediaPhoto) and isinstance(media.photo, types.Photo):
        return f'photo:{media.photo.id}:{bool(media.spoiler)}'
    if isinstance(media, types.MessageMediaDocument) and isinstance(media.document, types.Document):
        return f'document:{media.document.id}:{bool(media.spoiler)}'
    if media is None or isinstance(media, types.MessageMediaWebPage):
        return 'text'
    raise ValueError('TDL 回退暂不支持该媒体类型，未降级成文字或拆散相册')


async def source_posts(client, peer, ids, grouped_id):
    """Read actual grouped IDs, never infer membership from adjacent numeric IDs."""
    if not ids or len(ids) > 10 or max(ids) - min(ids) > 1000:
        raise ValueError('源消息范围异常，未发送')
    if grouped_id is None:
        posts = await client.get_messages(peer, ids=ids)
    else:
        # Telegram albums have <=10 members. Leave room for interleaved channel posts.
        low, high = max(1, min(ids) - 100), max(ids) + 100
        posts = await client.get_messages(peer, ids=list(range(low, high + 1)))
        posts = [p for p in posts if p and getattr(p, 'grouped_id', None) == grouped_id]
        if any(p.id in (low, high) for p in posts):
            raise ValueError('相册成员触及查询边界，未能安全确认整组，未发送')
    found = {p.id: p for p in posts if p and not isinstance(p, types.MessageEmpty)}
    if not set(ids).issubset(found):
        raise ValueError('源消息已删除或相册成员不可读取，未发送')
    posts = [found[mid] for mid in sorted(found)]
    if grouped_id is not None and not 2 <= len(posts) <= 10:
        raise ValueError('相册成员不完整或数量异常，未拆散发送')
    if any(getattr(p, 'grouped_id', None) != grouped_id for p in posts):
        raise ValueError('源消息分组已变化，未发送')
    for post in posts:
        media_key(post)
        if media_key(post) == 'text' and not post.raw_text:
            raise ValueError('源文字为空，未发送')
    if grouped_id is not None and any(media_key(p) == 'text' for p in posts):
        raise ValueError('相册含不可上传的成员，未发送')
    return posts


def expected_size(post):
    media = post.media
    if isinstance(media, types.MessageMediaDocument):
        return media.document.size
    if isinstance(media, types.MessageMediaPhoto):
        # Match upstream TDL GetPhotoSize: use the last (full-sized) photo variant.
        size = media.photo.sizes[-1] if media.photo.sizes else None
        return getattr(size, 'size', 0) or max(getattr(size, 'sizes', []) or [0])
    return 0


async def uploaded_media(tdl, client, target, source_id, post, directory):
    key = media_key(post)
    if key == 'text':
        return None
    path = Path(directory) / f'{post.id}.bin'
    await tdl.download_media(f'https://t.me/c/{source_id}/{post.id}', str(path))
    size = expected_size(post)
    if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
        raise ValueError(f'源消息 {post.id} 的媒体下载不完整，未发送')
    if size and path.stat().st_size != size:
        raise ValueError(f'源消息 {post.id} 的媒体大小不符，未发送')
    media = post.media
    file = await client.upload_file(str(path))
    if key.startswith('photo:'):
        uploaded = types.InputMediaUploadedPhoto(file, spoiler=media.spoiler)
    else:
        doc = media.document
        uploaded = types.InputMediaUploadedDocument(file, doc.mime_type, doc.attributes,
                                                    spoiler=media.spoiler)
    result = await client(functions.messages.UploadMediaRequest(target, uploaded))
    result = utils.get_input_media(result)
    if not isinstance(result, (types.InputMediaPhoto, types.InputMediaDocument)):
        raise ValueError(f'源消息 {post.id} 的媒体上传未返回有效引用')
    result.spoiler = bool(media.spoiler)
    return result


def publish_request(peer, posts, media, reserved):
    if len(posts) > 1:
        return functions.messages.SendMultiMediaRequest(peer, [
            types.InputSingleMedia(media[p.id], message=p.raw_text or '', random_id=reserved[p.id], entities=p.entities or [])
            for p in posts])
    post = posts[0]
    kwargs = dict(peer=peer, message=post.raw_text or '', random_id=reserved[post.id], entities=post.entities or [])
    if media[post.id] is None:
        return functions.messages.SendMessageRequest(**kwargs,
            no_webpage=not isinstance(post.media, types.MessageMediaWebPage))
    return functions.messages.SendMediaRequest(**kwargs, media=media[post.id])


def receipt_pairs(sent, reserved):
    by_random = {}
    for update in getattr(sent, 'updates', ()):
        if isinstance(update, types.UpdateMessageID) and update.random_id in reserved.values():
            if update.random_id in by_random and by_random[update.random_id] != update.id:
                raise ValueError('目标回执包含冲突的 random_id')
            if type(update.id) is not int or update.id <= 0:
                raise ValueError('目标回执消息 ID 无效')
            by_random[update.random_id] = update.id
    if isinstance(sent, types.UpdateShortSentMessage) and len(reserved) == 1:
        if type(sent.id) is int and sent.id > 0:
            by_random[next(iter(reserved.values()))] = sent.id
    pairs = {mid: by_random[rid] for mid, rid in reserved.items() if rid in by_random}
    if len(set(pairs.values())) != len(pairs):
        raise ValueError('目标回执将多个源成员关联到同一消息')
    return pairs
