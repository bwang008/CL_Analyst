import os
import re
import base64
import sys

def embed_images_in_html(input_html_path, output_html_path):
    """
    Reads an HTML file, finds all local <img> tags, and embeds the referenced
    images directly into the HTML as base64 data URIs.
    """
    if not os.path.exists(input_html_path):
        print(f"Error: Input HTML file not found at {input_html_path}")
        return False
        
    base_dir = os.path.dirname(os.path.abspath(input_html_path))
    
    with open(input_html_path, 'r', encoding='utf-8') as f:
        html_content = f.read()
        
    # Find all img tags and extract their src
    # This regex is flexible enough to capture src in double quotes, single quotes, or unquoted
    img_pattern = re.compile(r'(<img\s+[^>]*src=["\'])([^"\']+\.(?:png|jpg|jpeg|gif|svg))(["\'][^>]*>)', re.IGNORECASE)
    
    def replace_src(match):
        prefix = match.group(1)
        img_src = match.group(2)
        suffix = match.group(3)
        
        # Check if the image source is already a web URL or data URI
        if img_src.startswith(('http://', 'https://', 'data:')):
            print(f"Skipping already external/data URI: {img_src}")
            return match.group(0)
            
        # Resolve path relative to the HTML file
        # Handle file:/// prefixes or relative links
        clean_src = img_src
        if clean_src.startswith('file:///'):
            # Convert URI back to local file path
            clean_src = clean_src.replace('file:///', '')
            # On windows, file:///C:/path -> C:/path
            if os.name == 'nt' and clean_src[1] == ':':
                pass # Already absolute Windows path
            else:
                # Add root slash back for Unix-like absolute paths
                clean_src = '/' + clean_src
                
        # If the path is not absolute, make it absolute relative to the HTML folder
        if not os.path.isabs(clean_src):
            img_path = os.path.join(base_dir, clean_src)
        else:
            img_path = clean_src
            
        # If relative resolution failed, check common alternative directories (e.g. artifacts directory or parent)
        if not os.path.exists(img_path):
            # Try workspace root
            workspace_root = os.path.abspath(os.path.join(base_dir, "..", ".."))
            alt_path = os.path.join(workspace_root, clean_src)
            if os.path.exists(alt_path):
                img_path = alt_path
            else:
                # Try just the base filename in the HTML directory
                basename = os.path.basename(clean_src)
                alt_path2 = os.path.join(base_dir, basename)
                if os.path.exists(alt_path2):
                    img_path = alt_path2

        if not os.path.exists(img_path):
            print(f"Warning: Image file not found: {img_src} (Resolved as: {img_path})")
            return match.group(0)
            
        # Determine the MIME type
        ext = os.path.splitext(img_path)[1].lower()
        mime_type = "image/png"
        if ext in ['.jpg', '.jpeg']:
            mime_type = "image/jpeg"
        elif ext == '.gif':
            mime_type = "image/gif"
        elif ext == '.svg':
            mime_type = "image/svg+xml"
            
        try:
            with open(img_path, 'rb') as img_file:
                encoded_bytes = base64.b64encode(img_file.read())
                base64_str = encoded_bytes.decode('utf-8')
                
            data_uri = f"data:{mime_type};base64,{base64_str}"
            print(f"Successfully embedded: {img_src} ({len(base64_str)} chars)")
            return f"{prefix}{data_uri}{suffix}"
        except Exception as e:
            print(f"Error encoding image {img_path}: {e}")
            return match.group(0)
            
    # Perform replacement
    new_html_content = img_pattern.sub(replace_src, html_content)
    
    # Ensure target folder exists
    output_dir = os.path.dirname(os.path.abspath(output_html_path))
    os.makedirs(output_dir, exist_ok=True)
    
    with open(output_html_path, 'w', encoding='utf-8') as f:
        f.write(new_html_content)
        
    print(f"Self-contained HTML report successfully saved to:\n  {output_html_path}\n")
    return True

if __name__ == "__main__":
    if len(sys.argv) < 3:
        # Default paths if run directly without arguments
        default_in = os.path.join(os.path.dirname(__file__), "..", "reports", "prediction_distributions", "report_export.html")
        default_out = os.path.join(os.path.dirname(__file__), "..", "reports", "prediction_distributions", "report_export_self_contained.html")
        embed_images_in_html(os.path.abspath(default_in), os.path.abspath(default_out))
    else:
        embed_images_in_html(sys.argv[1], sys.argv[2])
