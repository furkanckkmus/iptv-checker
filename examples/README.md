# Örnek Dosyalar / Example Files

Bu klasördeki tüm adresler, kullanıcı adları ve şifreler **tamamen sahtedir**.
Hiçbir gerçek hesap veya yayın bilgisi içermez.

All addresses, usernames, and passwords in this directory are **completely fake**.
They do not contain any real account or stream information.

## Dosyalar / Files

| Dosya | Açıklama |
|---|---|
| `input.example.txt` | Örnek `input.txt` girdisi |
| `playlist.example.m3u` | Örnek M3U kanal listesi |
| `stream.example.m3u8` | Örnek HLS medya playlist'i |
| `sonuclar/` | `sonuclar/` klasörünün örnek çıktısı |
| `paneller/` | `paneller/` klasörünün örnek çıktısı |
| `output.example.txt` | Eski birleşik `output.txt` biçiminin örneği |

## Kullanım / Usage

```bash
cp examples/input.example.txt input.txt
python iptv_checker.py
```

Gerçek tarama sonuçları proje kökündeki `sonuclar/` ve `paneller/`
klasörlerine yazılır. Buradaki örnekler yalnızca biçimi göstermek içindir.

Real scan results are written to `sonuclar/` and `paneller/` in the project
root. The files here only demonstrate the output format.
