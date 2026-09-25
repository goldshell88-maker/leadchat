"""Снимки клиентов с CDN Авито — через наш nginx, с кэшем (12.09).

Из офиса и из-за VPN снимок с NN.img.avito.st грузится 3–6 с (замер с
ноутбука владельца: «фото не грузятся» — плитки так и стояли тёмными), с
сервера — мгновенно. API подменяет ссылку на подписанный адрес нашего
nginx; хост зашит шаблоном location — чужой через него не уйдёт.
"""

from __future__ import annotations

import pathlib
import re

from app.services import media

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_ссылка_на_снимок_подменяется_подписанной_нашей() -> None:
    url = media.proxied_avito_image_url("https://40.img.avito.st/image/1/1.AbC-x_9.jpg?")
    assert url is not None
    путь, параметры = url.split("?")
    assert путь == "/api/v1/avito-img/40.img.avito.st/image/1/1.AbC-x_9.jpg"
    assert re.fullmatch(r"sig=[A-Za-z0-9_-]+&exp=\d+", параметры)


def test_чужие_и_непонятные_ссылки_не_трогаются() -> None:
    for url in (
        "https://evil.example.com/40.img.avito.st/x.jpg",
        "https://40.img.avito.st.evil.ru/x.jpg",
        "https://www.avito.ru/img/1.jpg",
        "https://40.img.avito.st/image/1/x.jpg?token=1",
        "http://40.img.avito.st/image/1/x.jpg",
        None,
        42,
    ):
        assert media.proxied_avito_image_url(url) is None, url


def test_sign_attachments_подменяет_только_снимки_авито() -> None:
    вложения = [
        {"kind": "image", "url": "https://90.img.avito.st/image/1/a.jpg?", "media_id": "m1"},
        {"kind": "voice", "url": "https://x.avito.st/v.mp3", "media_id": "m2"},
        {"kind": "image", "url": "https://cdn.example.com/a.jpg", "media_id": "m3"},
    ]
    out = media.sign_attachments(вложения)
    assert out[0]["url"].startswith("/api/v1/avito-img/90.img.avito.st/image/1/a.jpg?sig=")
    assert out[0]["media_id"] == "m1"
    assert out[1] == вложения[1] and out[2] == вложения[2]


def test_nginx_отдаёт_снимок_только_по_подписи_и_только_с_cdn_авито() -> None:
    conf = (ROOT / "docker/nginx/templates/leadchat.conf.template").read_text(encoding="utf-8")
    m = re.search(r'location ~ "\^/api/v1/avito-img/(.+?)" \{(.+?)\n    \}', conf, re.S)
    assert m, "нет location для снимков Авито"
    шаблон, тело = m.group(1), m.group(2)
    assert r"[0-9]{2}\.img\.avito\.st" in шаблон
    assert "secure_link $arg_sig,$arg_exp;" in тело
    assert 'secure_link_md5 "$secure_link_expires$uri ${MEDIA_SIGN_KEY}";' in тело
    assert "proxy_pass https://$img_host/$img_path;" in тело
    assert "proxy_cache avito_img;" in тело and "proxy_ssl_verify on;" in тело
    assert 'proxy_set_header Cookie "";' in тело
    nginx = (ROOT / "docker/nginx/nginx.conf").read_text(encoding="utf-8")
    assert "keys_zone=avito_img:" in nginx
    compose = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    assert "imgcache:/var/cache/nginx/avito" in compose
