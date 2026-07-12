Introduction
We are excited to introduce Qwen-Image-2512, the December update of Qwen-Image’s text-to-image foundational model. You are welcome to try the latest model at Qwen Chat . Compared to the base Qwen-Image model released in August, Qwen-Image-2512 features the following key improvements:

Enhanced Huamn Realism Qwen-Image-2512 significantly reduces the “AI-generated” look and substantially enhances overall image realism, especially for human subjects.
Finer Natural Detail Qwen-Image-2512 delivers notably more detailed rendering of landscapes, animal fur, and other natural elements.
Improved Text Rendering Qwen-Image-2512 improves the accuracy and quality of textual elements, achieving better layout and more faithful multimodal (text + image) composition.
Model Performance
We conducted over 10,000 rounds of blind model evaluations on AI Arena , and the results show that Qwen-Image-2512 is currently the strongest open-source model—while remaining highly competitive even among closed-source models.so 

pip install git+https://github.com/huggingface/diffusers



from diffusers import DiffusionPipeline
import torch

model_name = "Qwen/Qwen-Image-2512"

# Load the pipeline
if torch.cuda.is_available():
    torch_dtype = torch.bfloat16
    device = "cuda"
else:
    torch_dtype = torch.float32
    device = "cpu"

pipe = DiffusionPipeline.from_pretrained(model_name, torch_dtype=torch_dtype).to(device)

# Generate image
prompt = '''A 20-year-old East Asian girl with delicate, charming features and large, bright brown eyes—expressive and lively, with a cheerful or subtly smiling expression. Her naturally wavy long hair is either loose or tied in twin ponytails. She has fair skin and light makeup accentuating her youthful freshness. She wears a modern, cute dress or relaxed outfit in bright, soft colors—lightweight fabric, minimalist cut. She stands indoors at an anime convention, surrounded by banners, posters, or stalls. Lighting is typical indoor illumination—no staged lighting—and the image resembles a casual iPhone snapshot: unpretentious composition, yet brimming with vivid, fresh, youthful charm.'''

negative_prompt = "低分辨率，低画质，肢体畸形，手指畸形，画面过饱和，蜡像感，人脸无细节，过度光滑，画面具有AI感。构图混乱。文字模糊，扭曲。"


# Generate with different aspect ratios
aspect_ratios = {
    "1:1": (1328, 1328),
    "16:9": (1664, 928),
    "9:16": (928, 1664),
    "4:3": (1472, 1104),
    "3:4": (1104, 1472),
    "3:2": (1584, 1056),
    "2:3": (1056, 1584),
}

width, height = aspect_ratios["16:9"]

image = pipe(
    prompt=prompt,
    negative_prompt=negative_prompt,
    width=width,
    height=height,
    num_inference_steps=50,
    true_cfg_scale=4.0,
    generator=torch.Generator(device="cuda").manual_seed(42)
).images[0]

image.save("example.png")

