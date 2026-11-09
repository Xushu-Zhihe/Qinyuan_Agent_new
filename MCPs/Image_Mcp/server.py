"""Describe a bounded image read; the trusted adapter owns bytes and evidence."""
from MCPs.fastmcp_compat import FastMCP

mcp = FastMCP('qinyuan-image-planner', json_response=True)


@mcp.tool(title='Read Sandbox Image')
def read_image(path: str) -> dict:
    """View PNG/JPEG/WebP or single-frame GIF pixels from an exact /workspace/input/ or
    /workspace/work/ file. This is a visual observation, not OCR or verified Fact.
    At most four recent images accompany a decision; current.images identifies
    the pixels actually supplied. Read an older image again to view it.
    """
    from Sandbox.image_readback import validate_image_path
    return {'image_read': {'path': validate_image_path(path)}}


if __name__ == '__main__':
    mcp.run(transport='stdio')
