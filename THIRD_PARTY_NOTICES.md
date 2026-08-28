# 第三方元件與模型公開聲明

AG-MONITOR Smart Video Screening v4.0.0 使用 Python 3.13 與第三方開放原始碼元件建置。完整可攜包保留各套件隨附的 `dist-info/licenses/`、著作權聲明與授權文字；本文件提供主要元件摘要，不取代各元件原始授權條款。

| 元件 | 發行版本 | 主要授權 | 官方來源 |
|---|---:|---|---|
| Ultralytics | 8.4.67 | AGPL-3.0 | <https://github.com/ultralytics/ultralytics> |
| YOLOv8／YOLO11／YOLO12 預訓練模型 | 官方 `v8.3.0` 資產 | AGPL-3.0／Ultralytics Enterprise | <https://github.com/ultralytics/assets/releases/tag/v8.3.0> |
| PyTorch | 2.13.0 CPU | Apache-2.0 與隨附第三方授權 | <https://pytorch.org/> |
| TorchVision | 0.28.0 | BSD | <https://github.com/pytorch/vision> |
| OpenCV Python | 4.13.0.92 | Apache-2.0 | <https://github.com/opencv/opencv-python> |
| PyAV | 17.1.0 | BSD-3-Clause | <https://github.com/PyAV-Org/PyAV> |
| Eel | 0.18.2 | MIT | <https://github.com/python-eel/Eel> |
| LAP | 0.5.13 | BSD-2-Clause | <https://github.com/gatagat/lap> |
| NumPy | 2.5.2 | BSD-3-Clause 與隨附第三方授權 | <https://numpy.org/> |
| SciPy | 1.18.1 | BSD-3-Clause 與隨附第三方授權 | <https://scipy.org/> |

## 模型檔案

正式可攜包包含下列模型，建置器會在納入前驗證 SHA-256：

| 檔名 | SHA-256 |
|---|---|
| `yolov8n.pt` | `F59B3D833E2FF32E194B5BB8E08D211DC7C5BDF144B90D2C8412C47CCFC83B36` |
| `yolo11n.pt` | `0EBBC80D4A7680D14987A577CD21342B65ECFD94632BD9A8DA63AE6417644EE1` |
| `yolo11s.pt` | `85A76FE86DD8AFE384648546B56A7A78580C7CB7B404FC595F97969322D502D5` |
| `yolo12n.pt` | `419FF3DCA37D69BACC93A50FA0C186A1C6F9FE62FAE0F108B0872829689E9CA6` |
| `yolo12s.pt` | `E915C2C4286E3F6F8610EF106FA3F94A7B8C19B30ECCEDE5887E22C33EF75F58` |

## 使用與責任邊界

- 本專案與完整發行包依 AGPL-3.0 公開；再散布或修改時，應遵守 AGPL-3.0 與各第三方元件條款。
- Ultralytics 對專有、封閉或商業部署另提供 Enterprise 授權；使用者應依自身部署情境確認適用條款。
- AI 偵測結果僅供人工快篩，不構成身分確認、法律結論或對原始影音內容的保證。
