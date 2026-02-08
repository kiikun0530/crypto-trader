"""
Chronos-T5-Tiny → ONNX 変換スクリプト

使い方:
  pip install torch transformers chronos-forecasting optimum[onnxruntime]
  python scripts/convert_chronos_onnx.py

出力:
  models/chronos-onnx/ に ONNX モデル + tokenizer config を保存
"""
import os
import json
import sys

def main():
    output_dir = os.path.join(os.path.dirname(__file__), '..', 'models', 'chronos-onnx')
    os.makedirs(output_dir, exist_ok=True)

    print("=" * 60)
    print("Chronos-T5-Tiny → ONNX 変換")
    print("=" * 60)

    # Step 1: Chronos の tokenizer パラメータを抽出
    print("\n[1/3] Chronos tokenizer パラメータ抽出...")
    try:
        from chronos import ChronosPipeline
        import torch

        pipeline = ChronosPipeline.from_pretrained(
            "amazon/chronos-t5-tiny",
            device_map="cpu",
            torch_dtype=torch.float32,
        )

        # Chronos tokenizer の設定を保存
        tokenizer = pipeline.tokenizer
        tokenizer_config = {
            'n_tokens': int(tokenizer.config.n_tokens),
            'n_special_tokens': int(tokenizer.config.n_special_tokens),
            'pad_token_id': int(tokenizer.config.pad_token_id),
            'eos_token_id': int(tokenizer.config.eos_token_id),
            'use_eos_token': bool(tokenizer.config.use_eos_token),
        }

        # tokenizer の centers (bin の中心値) を抽出
        if hasattr(tokenizer, 'centers'):
            tokenizer_config['centers'] = tokenizer.centers.numpy().tolist()
        if hasattr(tokenizer, 'edges'):
            tokenizer_config['edges'] = tokenizer.edges.numpy().tolist()

        config_path = os.path.join(output_dir, 'tokenizer_config.json')
        with open(config_path, 'w') as f:
            json.dump(tokenizer_config, f, indent=2)
        print(f"  → tokenizer_config.json 保存完了")

    except Exception as e:
        print(f"  警告: Chronos tokenizer 抽出失敗: {e}")
        print("  → デフォルトパラメータを使用します")
        # Chronos-T5-Tiny のデフォルト値
        tokenizer_config = {
            'n_tokens': 4096,
            'n_special_tokens': 2,
            'pad_token_id': 0,
            'eos_token_id': 1,
            'use_eos_token': True,
        }
        config_path = os.path.join(output_dir, 'tokenizer_config.json')
        with open(config_path, 'w') as f:
            json.dump(tokenizer_config, f, indent=2)

    # Step 2: T5 モデルを ONNX にエクスポート
    print("\n[2/3] T5 モデル → ONNX エクスポート...")
    try:
        from optimum.onnxruntime import ORTModelForSeq2SeqLM
        from transformers import AutoConfig

        # Chronos は内部的に T5 なので、T5 として読み込み＆エクスポート
        onnx_model = ORTModelForSeq2SeqLM.from_pretrained(
            "amazon/chronos-t5-tiny",
            export=True,
            provider="CPUExecutionProvider",
        )
        onnx_model.save_pretrained(output_dir)
        print(f"  → ONNX モデル保存完了: {output_dir}")

    except Exception as e:
        print(f"  optimum エクスポート失敗: {e}")
        print("  手動エクスポートを試行...")

        # フォールバック: torch.onnx.export で手動エクスポート
        try:
            from transformers import T5ForConditionalGeneration
            import torch

            model = T5ForConditionalGeneration.from_pretrained("amazon/chronos-t5-tiny")
            model.eval()

            # Encoder
            encoder_path = os.path.join(output_dir, 'encoder_model.onnx')
            dummy_input = torch.randint(0, 4096, (1, 30))
            torch.onnx.export(
                model.encoder,
                (dummy_input,),
                encoder_path,
                input_names=['input_ids'],
                output_names=['last_hidden_state'],
                dynamic_axes={
                    'input_ids': {0: 'batch', 1: 'sequence'},
                    'last_hidden_state': {0: 'batch', 1: 'sequence'}
                },
                opset_version=14,
            )
            print(f"  → encoder_model.onnx 保存完了")

            # Decoder (初回ステップ用)
            encoder_output = model.encoder(dummy_input).last_hidden_state
            decoder_input = torch.tensor([[0]])  # pad token
            torch.onnx.export(
                model,
                {
                    'input_ids': dummy_input,
                    'decoder_input_ids': decoder_input,
                },
                os.path.join(output_dir, 'decoder_model.onnx'),
                input_names=['input_ids', 'decoder_input_ids'],
                output_names=['logits'],
                dynamic_axes={
                    'input_ids': {0: 'batch', 1: 'encoder_sequence'},
                    'decoder_input_ids': {0: 'batch', 1: 'decoder_sequence'},
                    'logits': {0: 'batch', 1: 'decoder_sequence'}
                },
                opset_version=14,
            )
            print(f"  → decoder_model.onnx 保存完了")

        except Exception as e2:
            print(f"  手動エクスポートも失敗: {e2}")
            sys.exit(1)

    # Step 3: ファイルサイズ確認
    print("\n[3/3] 出力ファイル確認:")
    total_size = 0
    for f in sorted(os.listdir(output_dir)):
        fpath = os.path.join(output_dir, f)
        if os.path.isfile(fpath):
            size_mb = os.path.getsize(fpath) / (1024 * 1024)
            total_size += size_mb
            print(f"  {f}: {size_mb:.1f} MB")
    print(f"  合計: {total_size:.1f} MB")

    print("\n✅ 変換完了！")
    print(f"出力先: {os.path.abspath(output_dir)}")


if __name__ == '__main__':
    main()
