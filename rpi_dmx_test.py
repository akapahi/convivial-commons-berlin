import asyncio
from pyartnet import ArtNetNode

async def main():
    node_ip = "192.168.1.20"
    async with ArtNetNode.create(node_ip) as node:
        universe = node.add_universe(0)

        # Light 1 = channels 1–10
        light1 = universe.add_channel(start=1, width=10)

        # Light 2 = channels 11–20
        light2 = universe.add_channel(start=11, width=10)

        # Light 3 = channels 21–30
        light3 = universe.add_channel(start=21, width=10)

        # Example: Set all to different colours
        # [Master, R, G, B, White, Amber, UV, Strobe, Macro, MacroSpeed]
        await light1.add_fade([255, 255, 0, 0, 0, 0, 0, 0, 0, 0], 0)  # Red
        await light2.add_fade([255, 0, 255, 0, 0, 0, 0, 0, 0, 0], 0)  # Green
        await light3.add_fade([255, 0, 0, 255, 0, 0, 0, 0, 0, 0], 0)  # Blue

        await asyncio.sleep(0.2)

asyncio.run(main())
