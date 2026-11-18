# AI-Boardroom

**İnsan denetimli çoklu model müzakeresi ve karar desteği**

AI-Boardroom, farklı yapay zekâ modellerini aynı tartışma panelinde bir araya getiren Python ve PyQt6 masaüstü uygulamasıdır. Konuyu, uzmanlık rollerini ve tur sayısını siz belirlersiniz. Katılımcılar gerekçeli görüşler sunar, birbirinin sorularını ele alır ve önerilere destek veya itiraz bildirir. Sonuçta konuşmaları ve dayanaklarını inceleyebileceğiniz bir rapor oluşur.

## Tartışma nasıl ilerler?

1. **Kurulu oluşturun.** Mimar, Eleştirmen veya Ürün Tasarımcısı gibi roller seçin. Her role bir model ve görev verin.
2. **İlk turu başlatın.** Katılımcılar sırayla konuşur. İlk başarılı yanıtı veren model ilgili role sabitlenir.
3. **Turlar arasında yön verin.** Program sonraki tur için bekler. Bir not ekleyebilir veya **İtiraz Gönder** ile yanıta bağlanabilen bir itiraz bırakabilirsiniz.
4. **Görüşleri karşılaştırın.** Her katılımcı bir önceki turun aynı konuşma kaydını alır. Bu turda gelen yeni yanıtlar ayrıca eklenir. Açık sorular, itirazlar ve görüş değişiklikleri **Müzakere Takibi** bölümünde görülebilir.
5. **Raporu inceleyin.** Seçilen turlar tamamlandığında rapor, uzlaşı kayıtlarını, açık ayrılıkları ve modelin sonraki adım önerisini gösterir. Uzlaşı için son turda tüm katılımcıların açık desteği ve çözülmemiş itiraz bulunmaması gerekir.

Sabit model sonraki bir turda yanıt veremezse diğer katılımcılar sırayla denenir. Eksik turda **Eksik Yanıtları Dene** ile aynı modeli yeniden çağırabilir veya **Modeli Devret** ile uygun bir aday seçebilirsiniz. Başarılı devir, eski ve yeni model kimlikleriyle kayda girer. Her modelin görüşü kendi adına izlenir.

## Kurulum

Windows ve Python 3.12 ile doğrulandı. PowerShell’de:

```powershell
git clone https://github.com/cetincevizcetoli/ai-board.git
cd ai-board
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
```

| Başlatıcı | Kullanım |
| --- | --- |
| `Demo.bat` | Örnek bir tartışmayı sahte API yanıtlarıyla çalıştırır. API anahtarı gerekmez. |
| `Baslat.bat` | Uygulamayı açar. Gerçek kullanımda sağlayıcı anahtarınızı **Ayarlar** bölümüne girin. |
| `Test.bat` | Çevrimdışı otomatik testleri çalıştırır. |

Komut satırından çalıştırmak için `.\.venv\Scripts\python.exe -m ai_boardroom` kullanabilirsiniz.

## İlk deneme

**Demo.bat** ile açıp örnek konuyu başlatın. Gerçek API ile denemek için **Ayarlar** bölümünde **Ücretsiz** katmanı seçin ve OpenRouter anahtarınızı girin. **Model Kütüphanesi → Kataloğu Yenile** ile güncel sohbet modellerine bakın. Ardından modeli ve tur sayısını seçip tartışmayı başlatın.

> **Örnek konu:** İlk sürümde küçük bir yapılacaklar listesi uygulaması için kullanıcı hesabı zorunlu olmalı mı? Kullanım kolaylığı, veri güvenliği ve geliştirme maliyeti açısından değerlendirin.

## Modeller ve kullanım

- **Ücretsiz katman** güncel katalogdan fiyatı doğrulanan OpenRouter metin modellerini kullanır. Ücretsiz oturumda ücretli modele otomatik geçiş kapalıdır.
- **Ücretli / diğer katman** ayrı anahtar ve açık etkinleştirme gerektirir. OpenRouter, OpenAI, Anthropic, Google Gemini ve Groq için bağlantı seçenekleri bulunur.
- **API Kullanımı / Bakiye** sekmesinde OpenRouter’ın bildirdiği günlük ücretsiz hak, erişilebilen kredi bakiyesi ve oturum çağrıları görünür. Kota, sağlayıcı kapasitesi ve zaman aşımı olayları model günlüğünde izlenebilir. Başarısız istekler de ücretsiz günlük kotaya sayılabilir. [OpenRouter limitleri](https://openrouter.ai/docs/api/reference/limits)

## Oturum kayıtları

Her oturum bir yerel klasöre kaydedilir. **Geçmiş Kayıtlar** üzerinden klasörü açabilir veya raporu Markdown olarak dışa aktarabilirsiniz.

| Dosya | İçerik |
| --- | --- |
| `transcript.json` | Konuşmalar, tur durumu, görüşler, sorular, itirazlar, model devirleri ve API günlüğü |
| `ozet.md` | Tamamlanan oturumun kanıtları, nihai önerisi ve tam konuşma dökümü |
| `oturum_raporu.md` | Durdurulan veya eksik kalan oturumun mevcut kayıtları |

Varsayılan oturum yolu `%USERPROFILE%\.yz_panel\sessions`. Anahtarlar ve tercihler aynı profil klasöründeki `config.json` dosyasında saklanır. Gerçek API kullanıldığında ilgili konu ve konuşma bağlamı seçilen sağlayıcıya gönderilir. Sonuçları uygulamadan önce rapordaki gerekçeleri ve alıntılanan görüşleri inceleyin.

## Proje yapısı

```text
ai_boardroom/  Masaüstü arayüzü, müzakere motoru, model yönlendirme ve kayıt
tests/         Otomatik testler ve çevrimdışı Qt denemesi
tools/         Açıkça başlatılan gerçek API kontrol araçları
```

Geliştirme sırasında Qt arayüz testi için:

```powershell
.\.venv\Scripts\python.exe tests\gui_smoke.py
```

Bu test sahte API ve geçici kayıtlarla tur beklemeyi, kota sonrası yedek modeli ve kullanıcı onaylı model devrini sınar. `tools/` içindeki canlı kontroller gerçek API’ye erişir ve açıkça `--run` verilmesini gerektirir.
