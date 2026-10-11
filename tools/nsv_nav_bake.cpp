// Offline walkable-surface preparation. This program never executes guest code.
// Input: NSVCOL1\0, little-endian triangle count, six bounds floats, triangles.
// NSVCOL2 adds one allowed-floor byte per triangle and counted road quads.
// NSVCOL3 adds counted crosswalk triangles and water-surface triangles.
// NSVCOL4 adds counted authored pavement triangles after those areas.
// Output: NSVPOL2\0, vertex count, polygon count, nvp, world vertices, polygons, exact grid projected areas.
// Each output polygon stores nvp vertex indices then nvp Recast neighbor indices.
#include <Recast.h>
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <fstream>
#include <filesystem>
#include <iostream>
#include <memory>
#include <numeric>
#include <stdexcept>
#include <vector>

namespace {
void require(bool ok, const char* message) { if (!ok) throw std::runtime_error(message); }
template<class T> void read(std::istream& in, T* dst, size_t count) {
  require(bool(in.read(reinterpret_cast<char*>(dst), sizeof(T)*count)), "Truncated collision input");
}
template<class T> void write(std::ostream& out, const T* src, size_t count) {
  require(bool(out.write(reinterpret_cast<const char*>(src), sizeof(T)*count)), "Unable to write navigation output");
}
// Apply the actual triangle union, not merged mesh bounds or convex hulls.
// Crosswalks change only road spans. Water removes spans below its local
// surface, leaving beaches/decks above the water and dry areas outside it.
enum class Surface { Crossing, Water, Pavement };
void markTriangles(const std::vector<float>& triangles, Surface surface,
                   unsigned char road, unsigned char allowed, rcCompactHeightfield& chf) {
  for (size_t t=0;t<triangles.size();t+=9) {
    const float* a=triangles.data()+t; const float* b=a+3; const float* c=a+6;
    const double det=(b[2]-c[2])*(a[0]-c[0])+(c[0]-b[0])*(a[2]-c[2]);
    if (std::abs(det)<1e-6) continue;
    int x0=std::max(0,int(std::floor((std::min({a[0],b[0],c[0]})-chf.bmin[0])/chf.cs)));
    int x1=std::min(chf.width-1,int(std::floor((std::max({a[0],b[0],c[0]})-chf.bmin[0])/chf.cs)));
    int z0=std::max(0,int(std::floor((std::min({a[2],b[2],c[2]})-chf.bmin[2])/chf.cs)));
    int z1=std::min(chf.height-1,int(std::floor((std::max({a[2],b[2],c[2]})-chf.bmin[2])/chf.cs)));
    for (int z=z0;z<=z1;++z) for (int x=x0;x<=x1;++x) {
      const double px=chf.bmin[0]+(x+.5)*chf.cs, pz=chf.bmin[2]+(z+.5)*chf.cs;
      const double u=((b[2]-c[2])*(px-c[0])+(c[0]-b[0])*(pz-c[2]))/det;
      const double v=((c[2]-a[2])*(px-c[0])+(a[0]-c[0])*(pz-c[2]))/det;
      if (u<-1e-6 || v<-1e-6 || u+v>1.000001) continue;
      const double y=u*a[1]+v*b[1]+(1-u-v)*c[1];
      const auto& cell=chf.cells[x+z*chf.width];
      for (unsigned int i=cell.index;i<cell.index+cell.count;++i) {
        const double floor=chf.bmin[1]+chf.spans[i].y*chf.ch;
        if (surface==Surface::Water) {
          // The raster's span top rounds up by one height voxel.
          if (floor<=y+chf.ch) chf.areas[i]=RC_NULL_AREA;
        } else if (surface==Surface::Pavement && chf.areas[i]!=RC_NULL_AREA && std::abs(floor-y)<=.5) {
          chf.areas[i]=road;
        } else if (surface==Surface::Crossing && chf.areas[i]==road && std::abs(floor-y)<=.5) {
          chf.areas[i]=allowed;
        }
      }
    }
  }
}
}

#ifdef _WIN32
int wmain(int argc, wchar_t** argv) {
#else
int main(int argc, char** argv) {
#endif
  try {
    require(argc == 3 || argc == 4, "Usage: nsv_nav_bake collision.bin polygons.bin [--vehicle]");
#ifdef _WIN32
    const bool vehicle = argc == 4 && std::wstring(argv[3]) == L"--vehicle";
#else
    const bool vehicle = argc == 4 && std::string(argv[3]) == "--vehicle";
#endif
    require(argc == 3 || vehicle, "Unknown navigation bake mode");
    std::ifstream input(std::filesystem::path(argv[1]), std::ios::binary);
    std::array<char,8> magic{}; read(input,magic.data(),magic.size());
    const bool pavement = magic == std::array<char,8>{'N','S','V','C','O','L','4',0};
    const bool authored = pavement || magic == std::array<char,8>{'N','S','V','C','O','L','3',0};
    const bool classified = authored || magic == std::array<char,8>{'N','S','V','C','O','L','2',0};
    require(classified || magic == std::array<char,8>{'N','S','V','C','O','L','1',0}, "Invalid collision input version");
    uint32_t count; read(input,&count,1);
    require(count > 0 && count <= 5000000, "Collision triangle count outside bake limits");
    rcConfig cfg{};
    read(input,cfg.bmin,3); read(input,cfg.bmax,3);
    for (int i=0;i<3;++i)
      require(std::isfinite(cfg.bmin[i]) && std::isfinite(cfg.bmax[i]) &&
              cfg.bmin[i]<cfg.bmax[i], "Invalid bake bounds");
    std::vector<float> vertices(size_t(count)*9); read(input,vertices.data(),vertices.size());
    std::vector<unsigned char> floors(count,1);
    std::vector<float> roads, crossings, water, street;
    if (classified) {
      read(input,floors.data(),floors.size());
      for (auto area:floors) require(area<=1,"Invalid pedestrian floor classification");
      uint32_t quads; read(input,&quads,1);
      require(quads<=1000000,"Road footprint count outside bake limits");
      roads.resize(size_t(quads)*12); read(input,roads.data(),roads.size());
      for (float v:roads) require(std::isfinite(v),"Non-finite road footprint");
    }
    if (authored) for (auto* triangles:{&crossings,&water,&street}) {
      if (triangles==&street && !pavement) continue;
      uint32_t n; read(input,&n,1);
      require(n<=1000000,"Authored area triangle count outside bake limits");
      triangles->resize(size_t(n)*9); read(input,triangles->data(),triangles->size());
      for (float v:*triangles) require(std::isfinite(v),"Non-finite authored area triangle");
    }
    require(input.peek() == std::char_traits<char>::eof(), "Unexpected collision input tail");
    for (float v:vertices) require(std::isfinite(v), "Non-finite collision coordinate");
    // Match the native pedestrian radius, step and height. Raster resolution
    // is independent of the native runtime resource's query tolerance.
    cfg.cs=.20f; cfg.ch=.05f; cfg.walkableSlopeAngle=vehicle?20.f:45.f;
    cfg.walkableHeight=int(std::ceil((vehicle?2.f:1.6f)/cfg.ch));
    cfg.walkableClimb=int(std::floor((vehicle?.15f:.20f)/cfg.ch));
    cfg.walkableRadius=int(std::ceil((vehicle?1.5f:.35f)/cfg.cs));
    cfg.borderSize=cfg.walkableRadius+3;
    cfg.maxEdgeLen=int((vehicle?2.f:12.f)/cfg.cs); cfg.maxSimplificationError=vehicle?.25f:1.f;
    cfg.minRegionArea=64; cfg.mergeRegionArea=400; cfg.maxVertsPerPoly=6;
    rcCalcGridSize(cfg.bmin,cfg.bmax,cfg.cs,&cfg.width,&cfg.height);
    require(cfg.width>0 && cfg.height>0 && int64_t(cfg.width)*cfg.height<=16000000,
            "Bake raster exceeds memory limit");
    rcContext context;
    std::unique_ptr<rcHeightfield,decltype(&rcFreeHeightField)> solid(rcAllocHeightfield(),rcFreeHeightField);
    std::unique_ptr<rcCompactHeightfield,decltype(&rcFreeCompactHeightfield)> compact(rcAllocCompactHeightfield(),rcFreeCompactHeightfield);
    std::unique_ptr<rcContourSet,decltype(&rcFreeContourSet)> contours(rcAllocContourSet(),rcFreeContourSet);
    std::unique_ptr<rcPolyMesh,decltype(&rcFreePolyMesh)> mesh(rcAllocPolyMesh(),rcFreePolyMesh);
    require(solid && compact && contours && mesh,"Unable to allocate bake structures");
    require(rcCreateHeightfield(&context,*solid,cfg.width,cfg.height,cfg.bmin,cfg.bmax,cfg.cs,cfg.ch),"Heightfield creation failed");
    std::vector<unsigned char> areas(count,RC_NULL_AREA);
    std::vector<int> indices(size_t(count)*3); std::iota(indices.begin(),indices.end(),0);
    rcMarkWalkableTriangles(&context,cfg.walkableSlopeAngle,vertices.data(),int(count)*3,indices.data(),int(count),areas.data());
    // Keep forbidden floors as solid spans and a distinct temporary area.
    // Filtering must not resurrect them from a lower walkable span. After
    // compaction, discard that area before clearance erosion and contouring.
    // Recast merges coincident span areas with max(). Forbidden surfaces must
    // win that merge; otherwise an overlapping ordinary triangle revives them.
    constexpr unsigned char forbidden_area=RC_WALKABLE_AREA;
    constexpr unsigned char allowed_area=RC_WALKABLE_AREA-1;
    constexpr unsigned char road_area=RC_WALKABLE_AREA-2;
    for (size_t i=0;i<areas.size();++i)
      if (areas[i]!=RC_NULL_AREA) areas[i]=floors[i]?allowed_area:forbidden_area;
    require(rcRasterizeTriangles(&context,vertices.data(),areas.data(),int(count),*solid,cfg.walkableClimb),"Collision rasterization failed");
    rcFilterLowHangingWalkableObstacles(&context,cfg.walkableClimb,*solid);
    rcFilterLedgeSpans(&context,cfg.walkableHeight,cfg.walkableClimb,*solid);
    rcFilterWalkableLowHeightSpans(&context,cfg.walkableHeight,*solid);
    require(rcBuildCompactHeightfield(&context,cfg.walkableHeight,cfg.walkableClimb,*solid,*compact),"Compact heightfield failed");
    solid.reset();
    for (int i=0;i<compact->spanCount;++i)
      if (compact->areas[i]==forbidden_area) compact->areas[i]=RC_NULL_AREA;
    for (size_t i=0;i<roads.size();i+=12) {
      float min_y=roads[i+1],max_y=min_y;
      for (int j=1;j<4;++j) {
        min_y=std::min(min_y,roads[i+j*3+1]); max_y=std::max(max_y,roads[i+j*3+1]);
      }
      // Height-aware: the road under a bridge does not delete its walkway.
      rcMarkConvexPolyArea(&context,roads.data()+i,4,min_y-.5f,max_y+.5f,road_area,*compact);
    }
    markTriangles(street,Surface::Pavement,road_area,allowed_area,*compact);
    markTriangles(crossings,vehicle?Surface::Pavement:Surface::Crossing,road_area,allowed_area,*compact);
    for (int i=0;i<compact->spanCount;++i) {
      if (vehicle) compact->areas[i]=compact->areas[i]==road_area?allowed_area:RC_NULL_AREA;
      else if (compact->areas[i]==road_area) compact->areas[i]=RC_NULL_AREA;
    }
    markTriangles(water,Surface::Water,road_area,allowed_area,*compact);
    require(rcErodeWalkableArea(&context,cfg.walkableRadius,*compact),"Agent clearance erosion failed");
    require(rcBuildDistanceField(&context,*compact),"Distance field failed");
    require(rcBuildRegions(&context,*compact,cfg.borderSize,cfg.minRegionArea,cfg.mergeRegionArea),"Walkable region construction failed");
    require(rcBuildContours(&context,*compact,cfg.maxSimplificationError,cfg.maxEdgeLen,*contours),"Contour construction failed");
    require(rcBuildPolyMesh(&context,*contours,cfg.maxVertsPerPoly,*mesh),"Polygon construction failed");
    // Classify degeneracy on Recast's exact grid, before world-space float
    // rounding can turn collinear vertices into a tiny reversed triangle.
    std::vector<int64_t> projected_area(mesh->npolys);
    for (int p=0;p<mesh->npolys;++p) {
      const auto* poly=mesh->polys+size_t(p)*mesh->nvp*2;
      int n=0; while(n<mesh->nvp && poly[n]!=RC_MESH_NULL_IDX) ++n;
      for(int i=0;i<n;++i) {
        const auto* a=mesh->verts+size_t(poly[i])*3;
        const auto* b=mesh->verts+size_t(poly[(i+1)%n])*3;
        projected_area[p]+=int64_t(a[2])*b[0]-int64_t(a[0])*b[2];
      }
    }
    std::vector<float> world(size_t(mesh->nverts)*3);
    for (int i=0;i<mesh->nverts;++i) for (int a=0;a<3;++a)
      world[size_t(i)*3+a]=mesh->bmin[a]+mesh->verts[size_t(i)*3+a]*(a==1?mesh->ch:mesh->cs);
    std::ofstream output(std::filesystem::path(argv[2]),std::ios::binary|std::ios::trunc);
    const std::array<char,8> outmagic{'N','S','V','P','O','L','2',0};
    const std::array<uint32_t,3> sizes{uint32_t(mesh->nverts),uint32_t(mesh->npolys),uint32_t(mesh->nvp)};
    write(output,outmagic.data(),outmagic.size());write(output,sizes.data(),sizes.size());
    write(output,world.data(),world.size());write(output,mesh->polys,size_t(mesh->npolys)*mesh->nvp*2);
    write(output,projected_area.data(),projected_area.size());
    output.close();require(bool(output),"Unable to finish navigation output");
    std::cout<<count<<" collision triangles -> "<<mesh->npolys<<" walkable polygons\n";
    return 0;
  } catch (const std::exception& e) { std::cerr<<e.what()<<'\n';return 1; }
}
